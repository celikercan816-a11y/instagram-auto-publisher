"""Quote IMPACT engine (2026-09-01) -- a much stricter editorial gate added
ON TOP of quote_generator.py's banks and quote_quality.py's existing safety
checks (attribution, Turkish integrity, source classification), after the
user rejected the existing quote bank as reading like "random AI Instagram
quotes" -- explicit examples called out as weak: "Şehir unutmaz, sadece
biriktirir.", "Uzak yolun sonu, insanın kendisidir."

GOAL (verbatim from the brief): someone seeing the post should stop, read
it, think about the meaning, find something of themselves in it, want to
save it, want to send it to a friend. The quote comes first; the image
comes after.

HONEST LIMITATION, stated up front (same policy as every other
automated-but-imperfect check in this project): "emotional impact",
"original insight", "depth", "memorability" and "relatability" are NOT
computed by a formula from surface text features (word count, a sentiment
lexicon, etc.) -- faking that would be exactly the kind of hollow, inflated
precision this rework exists to eliminate. Those five dimensions of
QUOTE_QUALITY_SCORE are literary/editorial judgment calls, supplied at
candidate-authoring time via `editorial_scores` (by a human, or by an AI
actually reading and evaluating the line -- never derived mechanically).
What THIS module automates reliably:
  - flagging AI-cliche sentence openers (point 1) -- not an automatic
    rejection, but a flagged opener cannot coast to a top score without an
    exceptional, explicitly-justified continuation
  - flagging a small curated list of extremely well-known repackaged ideas
    (point 2) -- necessarily incomplete, a keyword heuristic, not true
    idea-extraction
  - approximate semantic-duplicate detection against quote history via
    content-word overlap (point 11) -- not true embeddings-based
    similarity, catches close paraphrases sharing most content words, not
    every possible rewording
  - enforcing the hard gates and a batch-level score-distribution sanity
    check (points 9-10): a candidate set where everything scores 90+ is
    treated as an evaluator failure, not a good sign
"""
import re
from dataclasses import dataclass, field

from src.quote_quality import check_turkish_text_integrity

IMPACT_WEIGHTS = {
    "emotional_impact": 20,
    "original_insight": 20,
    "memorability": 15,
    "relatability": 15,
    "natural_turkish": 10,
    "shareability": 10,
    "depth": 10,
}
IMPACT_APPROVAL_THRESHOLD = 85  # recalibrated 2026-09-01 -- see QUALITY_BANDS below

# Recalibration (round 3 was measured too harsh -- 400 candidates, 2 passers
# -- and the user was explicit: the goal is a high-quality, varied,
# shareable Instagram account, not "every line makes literary history").
# 85 is the real publish floor now; 88 is no longer a special binary gate,
# just the boundary between GOOD and STRONG.
QUALITY_BANDS = [
    (0, 69, "REJECT"),
    (70, 79, "RESERVE"),
    (80, 84, "GOOD"),
    (85, 89, "STRONG"),
    (90, 94, "PREMIUM"),
    (95, 100, "EXCEPTIONAL"),
]
PUBLISHABLE_BANDS = {"STRONG", "PREMIUM", "EXCEPTIONAL"}


def quality_band(score: int) -> str:
    for lo, hi, label in QUALITY_BANDS:
        if lo <= score <= hi:
            return label
    return "EXCEPTIONAL" if score > 100 else "REJECT"

# Point 1: not a ban -- "Bunlar tamamen yasak değil. Ancak yalnızca gerçekten
# sıra dışı bir devamı varsa kullanılabilir." Matched against the quote's
# FIRST clause only (up to the first comma/period/colon/newline), lowercased.
AI_CLICHE_OPENERS = [
    "bazen", "belki de", "insan bazen", "gece", "şehir unutmaz", "deniz",
    "yol", "sessizlik", "insan değişir", "zaman", "hayat bazen",
]

# Point 2: a small, necessarily-incomplete list of extremely well-known
# ideas whose repackaging shouldn't score as "original insight" just
# because the wording is new. All-keywords-present matching (crude but
# fast, no false-negative tolerance issue since this only ADDS scrutiny,
# never silently passes something worse through).
_COMMON_IDEA_SIGNATURES = [
    (["zaman", "değiştirir"], "'zaman her şeyi değiştirir' fikrinin yeniden paketlenmiş hali"),
    (["zaman", "iyileştirir"], "'zaman her şeyi iyileştirir' fikrinin yeniden paketlenmiş hali"),
    (["yaşadıkça", "öğren"], "'insan yaşadıkça öğrenir' fikrinin yeniden paketlenmiş hali"),
    (["her gece", "sabah"], "'her gecenin bir sabahı vardır' fikrinin yeniden paketlenmiş hali"),
    (["kimse", "görme", "ne yap"], "'karakter kimse görmezken ne yaptığındır' fikrinin (çok yaygın alıntı) yeniden paketlenmiş hali"),
    (["korkmamak", "değil"], "'cesaret korkmamak değildir' fikrinin çok yaygın yeniden paketlenmiş hali"),
    (["ışık", "karanlık", "ara"], "'karanlıkta ışık aramak' imgesinin çok yaygın kullanımı"),
]

_STOPWORDS = {
    "bir", "ve", "ile", "de", "da", "ki", "bu", "şu", "o", "için", "gibi",
    "kadar", "ama", "ancak", "değil", "olan", "olur", "olarak", "her",
    "sen", "seni", "senin", "ben", "beni", "benim", "en", "çok", "az",
}


def check_ai_cliche_opener(text: str) -> str | None:
    """Returns the matched flagged opener if the quote's first clause opens
    with one of the well-worn "trying to sound poetic" patterns, else None.
    Matches on the STEM (prefix match, not exact-word match) because
    Turkish is agglutinative -- "zamanla", "zamanı", "zamanın" all carry
    the same flagged opener "zaman" as their root, not just the bare word."""
    first_clause = re.split(r"[,.;:\n]", text.strip(), maxsplit=1)[0].strip().lower()
    for opener in AI_CLICHE_OPENERS:
        if first_clause.startswith(opener):
            return opener
    return None


def check_common_idea(text: str) -> str | None:
    lowered = text.lower()
    for keywords, label in _COMMON_IDEA_SIGNATURES:
        if all(k in lowered for k in keywords):
            return label
    return None


# Point 3 of the 2026-09-01 automation build: "Son 20 yayında aynı belirgin
# sentence pattern maksimum 2 kez" -- round 4's own winning batch leaned
# heavily on "Bir insan(ın)..." and comparative "X değil Y" structures, so
# these need explicit tracking once real publishing starts, not just at
# authoring time. Necessarily a heuristic (no syntactic parser) -- returns
# a SET of tags a line matches, since a quote can be both a "Bir insan..."
# opener AND an "X değil Y" comparison at once.
_OPENER_PATTERNS = {
    "BIR_INSAN": ["bir insan", "bir insanın", "bir insanı"],
    "INSAN": ["insan", "insanın", "insanı"],
    "BAZEN": ["bazen"],
    "GERCEK": ["gerçek"],
    "HAYAT": ["hayat"],
    "ZAMAN": ["zaman"],
    "OZGURLUK": ["özgürlük"],
    "SEVMEK": ["sevmek", "seni sevmek"],
}


def detect_sentence_patterns(text: str) -> set[str]:
    tags = set()
    first_clause = re.split(r"[,.;:\n]", text.strip(), maxsplit=1)[0].strip().lower()
    for tag, prefixes in _OPENER_PATTERNS.items():
        if any(first_clause.startswith(p) for p in prefixes):
            tags.add(tag)
    if re.search(r"\S+\s*değil\S*[,;]", text.lower()):
        tags.add("X_DEGIL_Y")
    if not tags:
        words = re.findall(r"[a-zçğıiöşü]+", first_clause)
        if words:
            tags.add(f"OPENER_{words[0].upper()}")
    return tags


def check_pattern_repetition(text: str, recent_texts: list[str], max_repeat: int = 2, window: int = 20) -> list[str]:
    """recent_texts: the last `window` PUBLISHED quote texts (order doesn't
    matter, only membership within the window)."""
    tags = detect_sentence_patterns(text)
    issues = []
    recent_window = recent_texts[-window:]  # most recent `window` entries, not the oldest -- recent_texts is oldest-first
    for tag in sorted(tags):
        count = sum(1 for t in recent_window if tag in detect_sentence_patterns(t))
        if count >= max_repeat:
            issues.append(f"Sentence pattern '{tag}' son {window} yayında zaten {count} kez kullanılmış (limit {max_repeat})")
    return issues


def _content_words(text: str) -> set[str]:
    words = re.findall(r"[a-zçğıiöşü]+", text.lower())
    return {w for w in words if w not in _STOPWORDS and len(w) > 2}


def check_semantic_duplicate(text: str, recent_texts: list[str], threshold: float = 0.55) -> tuple[bool, str | None, float]:
    """Point 11: "aynı fikrin farklı kelimelerle tekrarı da duplicate kabul
    edilebilsin." Approximate -- content-word Jaccard overlap, not real
    semantic embeddings (none available offline). Catches close
    paraphrases sharing most of their meaningful words; will miss a full
    conceptual rewrite that shares no vocabulary -- an honest limitation,
    not claimed as true semantic similarity."""
    words_a = _content_words(text)
    if not words_a:
        return False, None, 0.0
    best_ratio, best_match = 0.0, None
    for other in recent_texts:
        words_b = _content_words(other)
        if not words_b:
            continue
        overlap = len(words_a & words_b) / len(words_a | words_b)
        if overlap > best_ratio:
            best_ratio, best_match = overlap, other
    return best_ratio >= threshold, best_match, best_ratio


@dataclass
class ImpactVerdict:
    text: str
    total: int
    band: str
    breakdown: dict
    ai_cliche_opener: str | None
    common_idea_flag: str | None
    duplicate: bool
    duplicate_match: str | None
    duplicate_ratio: float
    turkish_issues: list[str]
    reject: bool
    reject_reasons: list[str] = field(default_factory=list)


def evaluate_quote_impact(text: str, editorial_scores: dict, recent_texts: list[str] | None = None,
                           cliche_override_justification: str | None = None) -> ImpactVerdict:
    """editorial_scores must supply all 7 IMPACT_WEIGHTS keys, each within
    0..that dimension's weight -- this is the literary-judgment input (see
    module docstring), not something this function derives on its own.

    cliche_override_justification: point 1 says a flagged opener is "not a
    ban", just something that needs a genuinely exceptional continuation to
    earn a top score. A flagged opener scoring >=IMPACT_APPROVAL_THRESHOLD
    is rejected UNLESS this is a non-empty string explaining exactly why
    this particular line clears that bar -- an explicit, inspectable
    editorial decision, never a silent pass."""
    for key, weight in IMPACT_WEIGHTS.items():
        if key not in editorial_scores or not (0 <= editorial_scores[key] <= weight):
            raise ValueError(f"editorial_scores['{key}'] must be an int in 0..{weight}")

    total = round(sum(editorial_scores.values()))
    opener = check_ai_cliche_opener(text)
    idea_flag = check_common_idea(text)
    is_dup, dup_match, dup_ratio = check_semantic_duplicate(text, recent_texts or [])
    turkish_issues = check_turkish_text_integrity(text)

    reasons = []
    if idea_flag:
        reasons.append(f"MEANINGLESS/klişe fikir: {idea_flag}")
    if is_dup:
        reasons.append(f"SEMANTIC_DUPLICATE (kelime örtüşmesi {dup_ratio:.2f}): '{dup_match}'")
    if turkish_issues:
        reasons.append(f"BAD_TURKISH: {turkish_issues}")
    if opener and total >= IMPACT_APPROVAL_THRESHOLD and not cliche_override_justification:
        reasons.append(f"AI_CLICHE opener ('{opener}') bu skorla geçemez -- istisna için açık editoryal gerekçe gerekli")

    reject = total < IMPACT_APPROVAL_THRESHOLD or bool(reasons)
    return ImpactVerdict(
        text=text, total=total, band=quality_band(total), breakdown=dict(editorial_scores),
        ai_cliche_opener=opener, common_idea_flag=idea_flag,
        duplicate=is_dup, duplicate_match=dup_match, duplicate_ratio=dup_ratio,
        turkish_issues=turkish_issues, reject=reject, reject_reasons=reasons,
    )


def check_score_distribution_realism(scores: list[int]) -> list[str]:
    """Point 10 (recalibrated 2026-09-01): the goal is a healthy PUBLISH
    rate now, not a punishing one -- round 3's 400-candidates/2-passers
    result was explicitly rejected as too harsh. This still catches actual
    inflation (almost everything passing) without fighting the intended
    higher pass rate at the new 85 floor. Flags a batch, doesn't reject
    individual quotes."""
    if len(scores) < 15:
        return []
    high = sum(1 for s in scores if s >= IMPACT_APPROVAL_THRESHOLD)
    if high > max(5, len(scores) // 3):
        return [f"{high}/{len(scores)} aday {IMPACT_APPROVAL_THRESHOLD}+ aldı -- puanlama şişirilmiş olabilir, gerçek bir dağılım bekleniyor"]
    return []
