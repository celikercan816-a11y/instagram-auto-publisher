"""Reads/writes content_queue.json.

Schema per item:
{
  "id": "uuid4 string",
  "media_type": "IMAGE" | "VIDEO" | "REELS" | "CAROUSEL",
  "media_url": "https://..." (str) or [str, ...] for CAROUSEL,
  "caption": "text with hashtags",
  "scheduled_at": "2026-09-01T19:30:00+03:00"  (ISO 8601, include UTC offset),
  "status": "pending" | "published" | "failed",
  "published_at": "2026-09-01T19:31:04+00:00" | null,
  "instagram_media_id": "17895..." | null,
  "error": "human readable reason" | null
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
) -> dict:
    dup = None if allow_duplicate else find_duplicate(items, media_type, media_url, caption)
    if dup:
        raise ValueError(
            f"Duplicate content detected (matches queue item {dup['id']}, status={dup['status']}). "
            "Pass allow_duplicate=True if this is intentional."
        )
    item = {
        "id": str(uuid.uuid4()),
        "media_type": media_type,
        "media_url": media_url,
        "caption": caption,
        "scheduled_at": scheduled_at,
        "status": "pending",
        "published_at": None,
        "instagram_media_id": None,
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
