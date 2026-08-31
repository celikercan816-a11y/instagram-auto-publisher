"""Reads/writes content_history.json -- the append-only record of published
content used for repetition checks (content_quality.py) and, later, performance
learning (performance.py).

Entry schema:
{
  "id": queue item id,
  "theme": "lifestyle" | "travel_landscape" | "style_fashion" | "motivation" | "reels",
  "content_type": "post" | "reels",
  "caption_summary": first ~120 chars of the caption (for quick similarity checks),
  "hashtags": ["#tag1", ...],
  "image_fingerprint": sha256 hex of the media file bytes | null,
  "published_at": ISO 8601,
  "instagram_media_id": str,
  "insights": {"reach": int, "likes": int, "comments": int, "saved": int,
               "shares": int, "engagement_rate": float} | null,
  "performance_score": 0-100 | null
}
"""
import hashlib
import json
from pathlib import Path

HISTORY_PATH = Path(__file__).resolve().parent.parent / "content_history.json"


def load_history(path: Path = HISTORY_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_history(entries: list[dict], path: Path = HISTORY_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entries, f, ensure_ascii=False, indent=2)
        f.write("\n")


def file_fingerprint(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record_published(item: dict, image_fingerprint: str | None) -> None:
    """Best-effort append; never raises, since a history-write hiccup must not
    fail an otherwise-successful publish (called from src/publisher.py)."""
    try:
        history = load_history()
        history.append({
            "id": item.get("id"),
            "theme": item.get("theme"),
            "content_type": item.get("content_type"),
            "caption_summary": (item.get("caption") or "")[:120],
            "hashtags": item.get("hashtags") or [],
            "image_fingerprint": image_fingerprint,
            "published_at": item.get("published_at"),
            "instagram_media_id": item.get("instagram_media_id"),
            "insights": None,
            "performance_score": None,
        })
        save_history(history)
    except Exception:
        pass


def recent(entries: list[dict], days: int, now=None) -> list[dict]:
    from datetime import datetime, timedelta, timezone

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(days=days)
    out = []
    for e in entries:
        ts = e.get("published_at")
        if not ts:
            continue
        dt = datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt >= cutoff:
            out.append(e)
    return out
