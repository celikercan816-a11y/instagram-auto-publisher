"""Reads/writes content_queue.json.

Core schema per item (unchanged since the manually-published test post -- the
publisher only ever reads media_type/media_url/caption/status/id, so this stays
stable regardless of what gets added below):
{
  "id": "uuid4 string",
  "media_type": "IMAGE" | "VIDEO" | "REELS" | "CAROUSEL",
  "media_url": "https://..." (str) or [str, ...] for CAROUSEL,
  "caption": "text with hashtags",
  "scheduled_at": "2026-09-01T19:30:00+03:00"  (ISO 8601, include UTC offset),
  "status": "pending" | "published" | "failed" | "needs_review" | "needs_generation",
  "published_at": "2026-09-01T19:31:04+00:00" | null,
  "instagram_media_id": "17895..." | null,
  "error": "human readable reason" | null
}

Extended fields added for the autonomous content manager (all optional, default
to a neutral value so every pre-existing item and every pre-existing caller of
add_item() keeps working unmodified):
{
  "content_type": "post" | "reels",
  "theme": "lifestyle" | "travel_landscape" | "style_fashion" | "motivation" | "reels" | null,
  "media_source": "local" | "ai_generated" | "manual" | null,
  "media_path": "media/xxx.jpg" (repo-relative) or [str, ...] | null,
  "image_prompt": "the prompt used for AI generation" | null,
  "hashtags": ["#tag1", "#tag2", ...],
  "quality_score": 0-100 | null,
  "created_at": ISO 8601,
  "performance_score": 0-100 | null (filled in later by src/performance.py)
}
"""
import hashlib
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

QUEUE_PATH = Path(__file__).resolve().parent.parent / "content_queue.json"


def load_queue(path: Path = QUEUE_PATH) -> list[dict]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_queue(items: list[dict], path: Path = QUEUE_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _content_fingerprint(media_type: str, media_url, caption: str) -> str:
    urls = media_url if isinstance(media_url, list) else [media_url]
    raw = media_type + "|" + "|".join(sorted(urls)) + "|" + (caption or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def find_duplicate(items: list[dict], media_type: str, media_url, caption: str) -> dict | None:
    """Guards against accidentally queueing the exact same media+caption twice
    (whether it's still pending or was already published)."""
    fp = _content_fingerprint(media_type, media_url, caption)
    for item in items:
        if item.get("status") == "failed":
            continue
        existing_fp = _content_fingerprint(item["media_type"], item["media_url"], item.get("caption", ""))
        if existing_fp == fp:
            return item
    return None


def add_item(
    items: list[dict],
    media_type: str,
    media_url,
    caption: str,
    scheduled_at: str,
    allow_duplicate: bool = False,
    *,
    content_type: str | None = None,
    theme: str | None = None,
    media_source: str | None = None,
    media_path=None,
    image_prompt: str | None = None,
    hashtags: list[str] | None = None,
    quality_score: int | None = None,
    status: str = "pending",
    item_id: str | None = None,
) -> dict:
    dup = None if (allow_duplicate or not media_url) else find_duplicate(items, media_type, media_url, caption)
    if dup:
        raise ValueError(
            f"Duplicate content detected (matches queue item {dup['id']}, status={dup['status']}). "
            "Pass allow_duplicate=True if this is intentional."
        )
    item = {
        "id": item_id or str(uuid.uuid4()),
        "content_type": content_type or ("reels" if media_type == "REELS" else "post"),
        "theme": theme,
        "media_type": media_type,
        "media_source": media_source,
        "media_path": media_path,
        "media_url": media_url,
        "image_prompt": image_prompt,
        "caption": caption,
        "hashtags": hashtags or [],
        "scheduled_at": scheduled_at,
        "status": status,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "published_at": None,
        "instagram_media_id": None,
        "quality_score": quality_score,
        "performance_score": None,
        "error": None,
    }
    items.append(item)
    return item


def get_due_items(items: list[dict], now: datetime | None = None) -> list[dict]:
    now = now or datetime.now(timezone.utc)
    due = []
    for item in items:
        if item.get("status") != "pending":
            continue
        scheduled = datetime.fromisoformat(item["scheduled_at"])
        if scheduled.tzinfo is None:
            scheduled = scheduled.replace(tzinfo=timezone.utc)
        if scheduled <= now:
            due.append(item)
    due.sort(key=lambda i: i["scheduled_at"])
    return due


def mark_published(item: dict, instagram_media_id: str) -> None:
    item["status"] = "published"
    item["published_at"] = datetime.now(timezone.utc).isoformat()
    item["instagram_media_id"] = instagram_media_id
    item["error"] = None


def mark_failed(item: dict, error: str) -> None:
    item["status"] = "failed"
    item["error"] = error
