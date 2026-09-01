"""Quality gate for the "manzara + söz" content pivot (2026-09-01, revised
same day after the first 3-post test batch showed inflated 100/100 scores
that didn't reflect real placement/readability problems). Combines
automated checks (this module) with the existing structural image check
(content_quality.check_image).

quality_score is now a WEIGHTED BREAKDOWN out of 100, not a flat
100-minus-penalties number (explicit instruction):
  Typography/readability   25
  Text placement            20
  Contrast                  15
  Quote/source validity     20
  Composition                10
  Repetition/originality     10

Independently of that score, HARD FAIL conditions (wrong/unverifiable
attribution, broken Turkish text, text that doesn't fit the frame) force
status="hard_failed" no matter how high the weighted score would be.
quality_score < APPROVAL_SCORE_THRESHOLD -> never auto-approved.

Honest limitation (documented, not hidden -- same policy as
image_generator.py's own documented gap): there is no free/local OCR or
watermark-detector wired in, so "arka planda anlamsız AI yazısı var mı" and
"watermark var mı" are NOT automatically verifiable here. The scene prompts
(src/quote_scenes.py) explicitly forbid both, and this module flags it as a
standing note for the human reviewer in scripts/approve_preview.py-style
flows -- it does not claim to verify it.
"""
import re

from src.content_history import last_n
from src.content_quality import check_image
from src.quote_generator import PROVERBS, PUBLIC_DOMAIN_QUOTES, quote_hash
from src.text_renderer import CANVAS_SIZE, CONTRAST_LUMINANCE_MAX

APPROVAL_SCORE_THRESHOLD = 85

_VERIFIED_PROVERB_HASHES = {quote_hash(q["text"]) for q in PROVERBS}

# Defense-in-depth (point 6): even though quote_generator.py's own bank never
# assigns these names, this catches it if a future edit to the bank -- or a
# hand-added item -- ever slips one in.
MISATTRIBUTION_RISK_NAMES = [
    "mevlana", "yunus emre", "nietzsche", "kafka", "bukowski",
    "cemal süreya", "cemal sureya", "can yücel", "can yucel",
]

_VERIFIED_PUBLIC_DOMAIN = {quote_hash(q["text"]): q["author"] for q in PUBLIC_DOMAIN_QUOTES}

UNVERIFIABLE_LOCALLY_NOTE = (
    "Otomatik kontrol edilemeyen (OCR/watermark tespiti local'de kurulu "
    "değil): arka planda anlamsız AI yazısı, watermark. Prompt bunları "
    "yasaklıyor ama sonucu insan gözüyle teyit etmek gerekiyor."
)


def check_attribution(quote: dict) -> list[str]:
    """HARD FAIL conditions -- point 6's "yanlış yazar atfetme" rule."""
    reasons = []
    author = quote.get("author")
    if not author:
        return reasons
    lname = author.lower()
    if any(name in lname for name in MISATTRIBUTION_RISK_NAMES):
        reasons.append(f"YÜKSEK YANLIŞ ATIF RİSKİ taşıyan bir isimle işaretlenmiş: '{author}'")
        return reasons
    h = quote_hash(quote["text"])
    if h not in _VERIFIED_PUBLIC_DOMAIN:
        reasons.append(f"'{author}' adı doğrulanmış public-domain listesinde olmayan bir söze atanmış")
    elif _VERIFIED_PUBLIC_DOMAIN[h] != author:
        reasons.append(f"Yazar uyuşmazlığı: doğrulanmış kayıt '{_VERIFIED_PUBLIC_DOMAIN[h]}', verilen '{author}'")
    return reasons


def check_source_classification(quote: dict) -> tuple[list[str], float]:
    """Point 2's 'kaynak sınıflandırması şüpheliyse ciddi puan düşmeli':
    returns (issues, score_0_to_1) for the quote/source-validity sub-score.
    A quote LABELED "PROVERB" whose exact text isn't in the independently-
    reviewed PROVERBS list is a classification error, not just a minor
    issue -- heavily penalized even though it carries no named author (so
    it isn't a hard-fail misattribution, just an unreliable category tag)."""
    category = quote.get("category")
    if category == "PROVERB" and quote_hash(quote["text"]) not in _VERIFIED_PROVERB_HASHES:
        return (["'PROVERB' olarak etiketlenmiş ama doğrulanmış atasözü listesinde yok -- sınıflandırma güvenilir değil"], 0.25)
    return ([], 1.0)


def check_turkish_text_integrity(text: str) -> list[str]:
    """HARD FAIL conditions -- point 19's "Türkçe karakter bozuk mu"."""
    issues = []
    if "�" in text:
        issues.append("Metinde bozuk karakter (mojibake, U+FFFD) tespit edildi")
    if re.search(r"(.)\1{4,}", text):
        issues.append("Metinde anormal karakter tekrarı var (olası bozukluk)")
    if re.search(r"[^\S\n]{3,}", text):
        issues.append("Metinde anormal boşluk dizisi var (olası bozukluk)")
    return issues


def check_render_fit(render_result) -> list[str]:
    """HARD FAIL condition -- point 19's "metin kesiliyor mu" /
    point 10's "uzun metni zorla küçültmek yerine reddet"."""
    if not render_result.fit_ok:
        return [render_result.rejection_reason or "Metin kadraja sığmıyor, satır/boyut sınırını aşıyor"]
    return []


def check_grid_crop_safety(render_result, canvas_size: tuple[int, int] = CANVAS_SIZE) -> list[str]:
    """Point 5: the final post stays 1080x1350, but Instagram's profile grid
    shows a CENTER-CROPPED SQUARE of it -- a layout perfectly safe on the
    full 4:5 canvas can still have text fall outside that square and get
    clipped in the grid view. Returns issues; evaluate_quote_post() below
    treats any issue here as disqualifying for pending_approval. Legacy
    render_quote() results don't track a text bbox (documented gap) and
    return [] -- can't verify, not claimed to have passed."""
    if render_result.text_top is None:
        return []
    w, h = canvas_size
    side = min(w, h)
    crop_top, crop_left = (h - side) // 2, (w - side) // 2
    crop_bottom, crop_right = crop_top + side, crop_left + side
    issues = []
    if render_result.text_top < crop_top:
        issues.append(f"Metin, Instagram profil grid'inin kare crop'unun üst sınırının {crop_top - render_result.text_top}px üstünde -- grid görünümünde üstten kesilir")
    if render_result.text_bottom > crop_bottom:
        issues.append(f"Metin, Instagram profil grid'inin kare crop'unun alt sınırının {render_result.text_bottom - crop_bottom}px altında -- grid görünümünde alttan kesilir")
    if render_result.text_left < crop_left:
        issues.append("Metin, grid kare crop'unun solundan taşıyor")
    if render_result.text_right > crop_right:
        issues.append("Metin, grid kare crop'unun sağından taşıyor")
    return issues


def check_contrast(render_result) -> list[str]:
    if not render_result.contrast_ok:
        return [f"Metin arkasındaki kontrast yetersiz (arka plan parlaklığı={render_result.bg_luminance_at_text:.0f})"]
    return []


# Threshold is generous (unlike composite_quality's OBJECT_LIFESTYLE
# zero-tolerance) -- a landscape/cityscape is allowed a few incidental
# pedestrians (point 3 says "no PROMINENT people", not zero people), this
# only flags a person/crowd that has become a large part of the frame.
PROMINENT_PEOPLE_COVERAGE_MAX = 0.15


def check_prominent_people(image_path: str) -> list[str]:
    """Local, best-effort (same rembg-based heuristic as
    src/person_composite.py and content_quality.check_unexpected_person).
    Silently returns [] if rembg isn't importable."""
    try:
        from PIL import Image

        from src.person_composite import _background_person_coverage
        coverage = _background_person_coverage(Image.open(image_path).convert("RGB"))
    except ImportError:
        return []
    if coverage > PROMINENT_PEOPLE_COVERAGE_MAX:
        return [f"Manzara görselinde beklenenden büyük insan/kalabalık kapsamı tespit edildi (kapsama={coverage:.2f} > {PROMINENT_PEOPLE_COVERAGE_MAX})"]
    return []


def check_quote_repetition(quote_hash_value: str, mood: str, template: str, history: list[dict], window: int = 50) -> list[str]:
    """Point 15: son 50 gönderide aynı söz/çok benzer tasarım tekrarını
    engelle. Exact-hash repetition of the quote text is a hard signal;
    repeating the same mood+template combination 3+ times in the last 10
    is a softer "getting repetitive" signal."""
    recent = last_n(history, window)
    issues = []
    used_hashes = {(e.get("attributes") or {}).get("quote_hash") for e in recent}
    if quote_hash_value in used_hashes:
        issues.append(f"Bu söz son {window} gönderide zaten kullanılmış")

    recent10 = last_n(history, 10)
    same_combo = sum(
        1 for e in recent10
        if (e.get("attributes") or {}).get("mood") == mood and (e.get("attributes") or {}).get("quote_template") == template
    )
    if same_combo >= 3:
        issues.append(f"Aynı ruh hali+template kombinasyonu ('{mood}'+'{template}') son 10 gönderide {same_combo} kez kullanılmış")
    return issues


# Weighted breakdown, sums to 100 -- explicit instruction, replaces the old
# flat "100 minus penalties" scheme that gave all 3 test posts 100/100
# regardless of a real placement problem in post #2.
WEIGHT_TYPOGRAPHY = 25
WEIGHT_PLACEMENT = 20
WEIGHT_CONTRAST = 15
WEIGHT_SOURCE = 20
WEIGHT_COMPOSITION = 10
WEIGHT_REPETITION = 10


def evaluate_quote_post(image_path: str, quote: dict, render_result, history: list[dict], template: str,
                         content_format: str = "feed") -> dict:
    """Single entry point. Returns quality_score (weighted breakdown, see
    module docstring) plus status/hard_fail like
    composite_quality.evaluate_composite(), and a `score_breakdown` dict
    showing exactly how the total was reached.

    content_format: "feed" (default, 1080x1350, checked against Instagram's
    square profile-grid crop) or "story" (1080x1920 -- stories never appear
    in the profile grid, so that check doesn't apply and is skipped)."""
    hard_fail_reasons: list[str] = []
    hard_fail_reasons += check_attribution(quote)
    hard_fail_reasons += check_turkish_text_integrity(quote["text"])
    hard_fail_reasons += check_render_fit(render_result)

    soft_issues: list[str] = []

    # -- Typography/readability (25): line-length balance is the main
    # measurable signal beyond what's already guaranteed by construction
    # (<=4 lines, min font size, safe margins).
    typography_score = WEIGHT_TYPOGRAPHY * (0.4 + 0.6 * render_result.line_length_balance)
    if render_result.line_length_balance < 0.6:
        soft_issues.append(f"Satır uzunlukları birbirinden farklı (denge={render_result.line_length_balance:.2f})")

    # -- Text placement (20): directly from _choose_zone()'s cleanliness
    # score -- this is what would have caught post #2's text-on-bridge issue.
    placement_score = WEIGHT_PLACEMENT * render_result.placement_score
    if render_result.placement_score < 0.6:
        soft_issues.append(f"Metin, görselin nispeten parlak/karmaşık bir bölgesine yerleşti (placement_score={render_result.placement_score:.2f})")

    # -- Contrast (15): graduated, not binary.
    if render_result.contrast_ok:
        contrast_score = WEIGHT_CONTRAST
    else:
        deficit = max(0.0, (render_result.bg_luminance_at_text - CONTRAST_LUMINANCE_MAX) / 125.0)
        contrast_score = WEIGHT_CONTRAST * max(0.0, 1.0 - deficit)
        soft_issues.append(f"Metin arkasındaki kontrast yetersiz (arka plan parlaklığı={render_result.bg_luminance_at_text:.0f})")

    # -- Quote/source validity (20): category/attribution trustworthiness.
    source_issues, source_ratio = check_source_classification(quote)
    soft_issues += source_issues
    source_score = WEIGHT_SOURCE * source_ratio

    # -- Composition (10): structural image health (resolution/aspect/corruption).
    img_issues = check_image(image_path, is_reels=(content_format == "story"))
    soft_issues += img_issues
    composition_score = max(0.0, WEIGHT_COMPOSITION - 4 * len(img_issues))

    people_issues = check_prominent_people(image_path)
    soft_issues += people_issues
    composition_score = max(0.0, composition_score - 5 * len(people_issues))

    # -- Grid-crop safety (point 5): FEED-only -- a story never appears in
    # Instagram's square profile grid, so this check doesn't apply to it.
    grid_crop_issues = check_grid_crop_safety(render_result) if content_format == "feed" else []
    soft_issues += grid_crop_issues

    # -- Repetition/originality (10).
    rep_issues = check_quote_repetition(quote["hash"], quote["mood"], template, history)
    soft_issues += rep_issues
    if any("zaten kullanılmış" in i for i in rep_issues):
        repetition_score = 0.0
    elif rep_issues:
        repetition_score = WEIGHT_REPETITION * 0.5
    else:
        repetition_score = WEIGHT_REPETITION

    breakdown = {
        "typography": round(typography_score, 1),
        "placement": round(placement_score, 1),
        "contrast": round(contrast_score, 1),
        "source_validity": round(source_score, 1),
        "composition": round(composition_score, 1),
        "repetition": round(repetition_score, 1),
    }
    score = round(sum(breakdown.values()))
    score = max(0, min(100, score))

    hard_fail = len(hard_fail_reasons) > 0
    # A HARD FAIL (overflow, safe-margin violation, broken Turkish text, bad
    # attribution) must never coexist with a score that looks acceptable --
    # a post that overflows the frame showing 88/100 is exactly the failure
    # mode this was built to prevent. Explicit instruction: overflow ->
    # quality_score = 0, hard_fail = true.
    if hard_fail:
        score = 0
        status = "hard_failed"
    elif render_result.placement_score < 0.6:
        # Placement < 12/20 alone must block auto-approval even if the total
        # clears APPROVAL_SCORE_THRESHOLD -- a high total must never paper
        # over text sitting on a busy/bright region. Explicit instruction:
        # total score >= 85 is not sufficient by itself.
        soft_issues.append(
            f"Yerleşim skoru eşiğin altında (placement_score={render_result.placement_score:.2f} < 0.60) "
            "-- toplam skor yeterli olsa bile otomatik onaya gönderilmiyor."
        )
        status = "needs_review"
    elif grid_crop_issues:
        # Point 5: "Grid crop'ta metin kesiliyorsa: PENDING_APPROVAL verme."
        status = "needs_review"
    elif score >= APPROVAL_SCORE_THRESHOLD:
        status = "pending_approval"
    else:
        status = "needs_review"

    return {
        "quality_score": score,
        "score_breakdown": breakdown,
        "quality_issues": soft_issues,
        "hard_fail": hard_fail,
        "hard_fail_reasons": hard_fail_reasons,
        "status": status,
        "unverifiable_note": UNVERIFIABLE_LOCALLY_NOTE,
    }
