"""Pre-publish quality gate. Produces a 0-100 score; content scoring below 70
must not be auto-published (src/content_planner.py enforces this by setting
status="needs_review" instead of "pending" when the score doesn't clear the
bar even after a light auto-fix attempt).
"""
import difflib
from pathlib import Path

from src.content_bank import CLICHE_PHRASES, SPAM_HASHTAGS, generate_caption, generate_hashtags
from src.content_history import recent

PROJECT_ROOT = Path(__file__).resolve().parent.parent

MIN_SHORT_SIDE_PX = 1080
FEED_ASPECT_RANGE = (0.8, 1.91)   # Instagram feed min/max width:height
REELS_ASPECT_RANGE = (0.5, 0.6)   # ~9:16
MAX_CAPTION_LEN = 2200
CAPTION_SIMILARITY_THRESHOLD = 0.85
HASHTAG_OVERLAP_THRESHOLD = 0.7
THEME_REPETITION_MAX_IN_30D = 6


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
    last_30d = recent(history, days=30)
    count = sum(1 for e in last_30d if e.get("theme") == theme)
    if count >= THEME_REPETITION_MAX_IN_30D:
        return [f"'{theme}' teması son 30 günde zaten {count} kez kullanıldı"]
    if last_30d and last_30d[-1].get("theme") == theme:
        return [f"Bir önceki paylaşım da '{theme}' temasıydı (art arda aynı tür)"]
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
            item["caption_text"] = generate_caption(theme, used_captions)
            item["caption"] = item["caption_text"] + "\n\n" + " ".join(item["hashtags"])
        score, issues = score_content(item, history, media_fingerprint)

    item["quality_score"] = score
    item["status"] = "pending" if score >= 70 else "needs_review"
    if score < 70:
        item["error"] = "Kalite kontrolden geçemedi: " + "; ".join(issues)
    return item
