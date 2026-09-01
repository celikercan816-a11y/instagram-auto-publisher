"""Pre-publish quality gate. Produces a 0-100 score; content scoring below 70
must not be auto-published (src/content_planner.py enforces this by setting
status="needs_review" instead of "pending" when the score doesn't clear the
bar even after a light auto-fix attempt).
"""
import difflib
from pathlib import Path

from src.content_bank import (
    ATTRIBUTE_FIELDS,
    CLICHE_PHRASES,
    SPAM_HASHTAGS,
    THEME_ALIASES,
    compose_caption,
    generate_caption,
    generate_hashtags,
    resolve_theme,
)
from src.content_history import last_n, recent

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIN_SHORT_SIDE_PX = 1080
FEED_ASPECT_RANGE = (0.8, 1.91)   # Instagram feed min/max width:height
REELS_ASPECT_RANGE = (0.5, 0.6)   # ~9:16
MAX_CAPTION_LEN = 2200
CAPTION_SIMILARITY_THRESHOLD = 0.85
HASHTAG_OVERLAP_THRESHOLD = 0.7
THEME_REPETITION_MAX_IN_30D = 6
ATTRIBUTE_OVERLAP_MAX = 3  # >=4 of 6 matching fields vs. a recent post is flagged

# Defense-in-depth for the "gerçek hayat iddiası" caution: content_bank's own
# caption/hashtag/location text never names a real club or claims attendance
# at a specific real match (spor_futbol content stays generic/atmosphere-only
# by design -- see content_bank.LOCATIONS["spor_futbol"]), but this catches it
# if a future edit to the bank -- or a hand-added item via scripts/add_to_queue.py
# -- ever slips one in.
REAL_CLUB_NAME_MARKERS = ["fenerbahçe", "fenerbahce", "galatasaray", "beşiktaş", "besiktas", "trabzonspor"]

# Shot types where NO person/body part at all should be visible in frame.
#
# lifestyle_detail_no_face/style_accessory_detail were RE-DEFINED 2026-09-01
# as OBJECT_LIFESTYLE: a coverage-based check previously couldn't tell
# "expected hand/arm closeup" apart from "unwanted face" for them (a
# legitimate hand+coffee-cup shot measured 60% coverage, since hands/arms
# ARE a person by pixel area) -- so the category itself was changed to ban
# every body part, not just the face (see content_bank.SHOT_TYPE_FRAMING).
# That makes this same coverage heuristic usable again: a pure object/
# still-life shot should measure ~0% person coverage.
NO_PROMINENT_PERSON_SHOT_TYPES = {"location_landscape_no_person", "lifestyle_detail_no_face", "style_accessory_detail"}
NO_PROMINENT_PERSON_THRESHOLDS = {
    "location_landscape_no_person": 0.02,
    "lifestyle_detail_no_face": 0.008,
    "style_accessory_detail": 0.008,
}


def check_image(path_str: str, is_reels: bool = False) -> list[str]:
    issues = []
    path = PROJECT_ROOT / path_str if not Path(path_str).is_absolute() else Path(path_str)
    try:
        from PIL import Image
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            w, h = img.size
    except Exception as e:
        return [f"Görsel açılamadı/bozuk: {e}"]

    short_side = min(w, h)
    if short_side < MIN_SHORT_SIDE_PX:
        issues.append(f"Çözünürlük düşük ({w}x{h}, kısa kenar {short_side}px < {MIN_SHORT_SIDE_PX}px)")

    ratio = w / h
    lo, hi = REELS_ASPECT_RANGE if is_reels else FEED_ASPECT_RANGE
    if not (lo <= ratio <= hi):
        issues.append(f"En-boy oranı Instagram için uygun değil ({w}x{h}, oran {ratio:.2f}, beklenen {lo}-{hi})")

    return issues


def check_caption(caption: str, history_entries: list[dict]) -> list[str]:
    issues = []
    text = (caption or "").strip()
    if not text:
        issues.append("Caption boş")
        return issues
    if len(text) > MAX_CAPTION_LEN:
        issues.append(f"Caption çok uzun ({len(text)} > {MAX_CAPTION_LEN})")

    lower = text.lower()
    for phrase in CLICHE_PHRASES:
        if phrase in lower:
            issues.append(f"Klişe motivasyon ifadesi içeriyor: '{phrase}'")
    for marker in REAL_CLUB_NAME_MARKERS:
        if marker in lower:
            issues.append(f"Gerçek kulüp/marka adı içeriyor (üretilen içerik gerçek bir etkinliğe katılımı ima etmemeli): '{marker}'")

    for entry in history_entries:
        prev = entry.get("caption_summary") or ""
        if not prev:
            continue
        ratio = difflib.SequenceMatcher(None, lower[:120], prev.lower()).ratio()
        if ratio >= CAPTION_SIMILARITY_THRESHOLD:
            issues.append(f"Caption daha önce kullanılana çok benziyor (benzerlik {ratio:.2f}, media_id={entry.get('instagram_media_id')})")
            break

    return issues


def check_hashtags(hashtags: list[str], history_entries: list[dict]) -> list[str]:
    issues = []
    n = len(hashtags or [])
    if n < 4 or n > 8:
        issues.append(f"Hashtag sayısı uygun değil ({n}, beklenen 4-8)")
    if len(set(hashtags)) != n:
        issues.append("Aynı hashtag birden fazla kez kullanılmış")
    spam = set(hashtags or []) & SPAM_HASHTAGS
    if spam:
        issues.append(f"Spam/engagement-bait hashtag kullanılmış: {sorted(spam)}")

    for entry in history_entries:
        prev = set(entry.get("hashtags") or [])
        if not prev:
            continue
        overlap = len(set(hashtags) & prev) / max(len(prev), 1)
        if overlap > HASHTAG_OVERLAP_THRESHOLD:
            issues.append(f"Hashtag seti son kullanılanla çok örtüşüyor (%{overlap*100:.0f}, media_id={entry.get('instagram_media_id')})")
            break

    return issues


def check_media_reuse(media_fingerprint: str | None, history_entries: list[dict]) -> list[str]:
    if not media_fingerprint:
        return []
    for entry in history_entries:
        if entry.get("image_fingerprint") == media_fingerprint:
            return [f"Bu görsel daha önce paylaşılmış (media_id={entry.get('instagram_media_id')})"]
    return []


def check_theme_repetition(theme: str | None, history: list[dict]) -> list[str]:
    if not theme:
        return []
    theme = resolve_theme(theme)
    last_30d = recent(history, days=30)

    def norm(t):
        return THEME_ALIASES.get(t, t)

    count = sum(1 for e in last_30d if norm(e.get("theme")) == theme)
    if count >= THEME_REPETITION_MAX_IN_30D:
        return [f"'{theme}' teması son 30 günde zaten {count} kez kullanıldı"]
    if last_30d and norm(last_30d[-1].get("theme")) == theme:
        return [f"Bir önceki paylaşım da '{theme}' temasıydı (art arda aynı tür)"]
    return []


def check_attribute_repetition(attributes: dict | None, history: list[dict]) -> list[str]:
    """Flags a post whose theme/location/outfit/pose/camera_angle/time_of_day
    combination overlaps too closely (>=4 of 6 fields) with one of the last 10
    published posts -- e.g. catches 'Boğaz + siyah tişört + yan profil + gece'
    coming back right after it was just used. Safety net alongside
    content_bank.generate_content_attributes's own resampling -- this also
    catches hand-added items (e.g. via scripts/add_to_queue.py) that bypassed
    that resampling entirely."""
    if not attributes:
        return []
    recent10 = last_n(history, 10)
    worst = 0
    for entry in recent10:
        other = entry.get("attributes") or {}
        overlap = sum(
            1 for f in ATTRIBUTE_FIELDS
            if attributes.get(f) is not None and attributes.get(f) == other.get(f)
        )
        worst = max(worst, overlap)
    if worst > ATTRIBUTE_OVERLAP_MAX:
        fields = ", ".join(f"{f}={attributes.get(f)}" for f in ATTRIBUTE_FIELDS if attributes.get(f))
        return [f"İçerik kombinasyonu son 10 paylaşımdan biriyle çok örtüşüyor ({fields}, örtüşen alan sayısı {worst})"]
    return []


def check_unexpected_person(media_path, shot_type: str | None) -> list[str]:
    """Best-effort, local-only safety net: flags a generated image whose main
    subject is a prominent person when shot_type says there shouldn't be one
    (location_landscape_no_person) or shouldn't show a face
    (lifestyle_detail_no_face/style_accessory_detail). Reuses the same
    rembg-based person-coverage heuristic src/person_composite.py already
    uses to retry a background generation. Silently returns [] if rembg
    isn't installed (e.g. in GitHub Actions, which never has the
    requirements-local-composite.txt extras) or the file doesn't exist --
    this is a nice-to-have extra check, never a hard dependency of the core
    pipeline."""
    if shot_type not in NO_PROMINENT_PERSON_SHOT_TYPES or not media_path:
        return []
    path_str = media_path[0] if isinstance(media_path, list) else media_path
    path = PROJECT_ROOT / path_str if not Path(path_str).is_absolute() else Path(path_str)
    if not path.exists():
        return []
    try:
        from PIL import Image

        from src.person_composite import _background_person_coverage
        coverage = _background_person_coverage(Image.open(path).convert("RGB"))
    except ImportError:
        return []
    threshold = NO_PROMINENT_PERSON_THRESHOLDS[shot_type]
    if coverage > threshold:
        return [f"'{shot_type}' için beklenmeyen büyüklükte insan/yüz figürü tespit edildi (kapsama={coverage:.3f} > {threshold})"]
    return []


def score_content(item: dict, history: list[dict], media_fingerprint: str | None = None) -> tuple[int, list[str]]:
    is_reels = item.get("media_type") == "REELS"
    issues: list[str] = []
    penalties = 0

    media_path = item.get("media_path")
    path_for_check = media_path[0] if isinstance(media_path, list) else media_path
    if path_for_check and (PROJECT_ROOT / path_for_check).exists():
        img_issues = check_image(path_for_check, is_reels=is_reels)
        issues += img_issues
        penalties += 40 if any("bozuk" in i for i in img_issues) else 20 * len(img_issues)

    cap_issues = check_caption(item.get("caption", ""), history)
    issues += cap_issues
    for i in cap_issues:
        penalties += 25 if "benziyor" in i else 15

    hash_issues = check_hashtags(item.get("hashtags") or [], history)
    issues += hash_issues
    penalties += 10 * len(hash_issues)

    reuse_issues = check_media_reuse(media_fingerprint, history)
    issues += reuse_issues
    penalties += 30 * len(reuse_issues)

    theme_issues = check_theme_repetition(item.get("theme"), history)
    issues += theme_issues
    penalties += 15 * len(theme_issues)

    attr_issues = check_attribute_repetition(item.get("attributes"), history)
    issues += attr_issues
    penalties += 20 * len(attr_issues)

    person_issues = check_unexpected_person(item.get("media_path"), (item.get("attributes") or {}).get("shot_type"))
    issues += person_issues
    penalties += 60 * len(person_issues)

    score = max(0, min(100, 100 - penalties))
    return score, issues


def run_quality_control(item: dict, history: list[dict], media_fingerprint: str | None = None) -> dict:
    """Scores the item; if below 70, tries one mechanical auto-fix pass
    (reshuffle hashtags, swap caption for another bank entry) and rescores.
    Sets item['quality_score'] and item['status'] ('pending' if it clears the
    bar, 'needs_review' otherwise). Returns the (mutated) item."""
    score, issues = score_content(item, history, media_fingerprint)

    if score < 70 and item.get("theme"):
        theme = item["theme"]
        used_captions = {e.get("caption_summary", "")[:len(item.get("caption", ""))] for e in history}
        if any("Hashtag" in i or "hashtag" in i for i in issues):
            recent_sets = [e.get("hashtags") or [] for e in recent(history, days=30) if e.get("theme") == theme]
            item["hashtags"] = generate_hashtags(theme, recent_sets)
        if any("Caption" in i or "caption" in i for i in issues if "Klişe" in i or "benziyor" in i):
            caption_text, caption_style = generate_caption(theme, used_captions, history=history, media_source=item.get("media_source"))
            item["caption_text"] = caption_text
            item.setdefault("attributes", {})["caption_style"] = caption_style
            item["caption"] = compose_caption(caption_text, item["hashtags"])
        score, issues = score_content(item, history, media_fingerprint)

    item["quality_score"] = score
    item["status"] = "pending" if score >= 70 else "needs_review"
    if score < 70:
        item["error"] = "Kalite kontrolden geçemedi: " + "; ".join(issues)
    return item
