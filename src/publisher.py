"""Entry point run on a schedule (GitHub Actions) to publish anything due in content_queue.json.

Exits with a non-zero status if anything failed, so the GitHub Actions run shows
as failed and GitHub's default email notification tells the repo owner something
went wrong (see the "error" field on the queue item and logs/publish_log.jsonl
for the exact reason).
"""
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config, ConfigError
from src.content_history import file_fingerprint, record_published
from src.instagram_api import InstagramAPIError, InstagramClient
from src.queue_manager import QUEUE_PATH, get_due_items, load_queue, mark_failed, mark_published, save_queue

LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "publish_log.jsonl"


def log_event(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False))


def publish_item(client: InstagramClient, item: dict) -> str:
    media_type = item["media_type"]
    media_url = item["media_url"]
    caption = item.get("caption", "")

    if media_type == "IMAGE":
        container_id = client.create_image_container(media_url, caption=caption)

    elif media_type == "STORIES":
        container_id = client.create_story_container(media_url)

    elif media_type in ("VIDEO", "REELS"):
        container_id = client.create_video_container(media_url, media_type=media_type, caption=caption)
        client.wait_until_ready(container_id)

    elif media_type == "CAROUSEL":
        if not isinstance(media_url, list) or len(media_url) < 2:
            raise ValueError("CAROUSEL items require media_url to be a list of 2-10 URLs")
        child_ids = []
        for url in media_url:
            is_video = url.lower().split("?")[0].endswith((".mp4", ".mov"))
            if is_video:
                cid = client.create_video_container(url, is_carousel_item=True)
                client.wait_until_ready(cid)
            else:
                cid = client.create_image_container(url, is_carousel_item=True)
            child_ids.append(cid)
        container_id = client.create_carousel_container(child_ids, caption=caption)

    else:
        raise ValueError(f"Unknown media_type: {media_type}")

    return client.publish(container_id)


def main() -> int:
    try:
        config = Config.load(require_token=True)
    except ConfigError as e:
        log_event({"level": "error", "message": f"Config error: {e}"})
        return 1

    client = InstagramClient(config)
    items = load_queue()
    due = get_due_items(items)

    if not due:
        log_event({"level": "info", "message": "No due items."})
        return 0

    try:
        limit = client.get_publishing_limit()
        quota_usage = limit.get("quota_usage", 0)
        quota_total = limit.get("config", {}).get("quota_total", 100)
    except InstagramAPIError as e:
        log_event({"level": "warning", "message": f"Could not fetch publishing limit, proceeding anyway: {e}"})
        quota_usage, quota_total = 0, 100

    had_failure = False

    for item in due:
        if quota_usage >= quota_total:
            msg = f"Daily publishing limit reached ({quota_usage}/{quota_total}). Will retry on next run."
            log_event({"level": "warning", "item_id": item["id"], "message": msg})
            break

        try:
            media_id = publish_item(client, item)
            mark_published(item, media_id)
            quota_usage += 1
            try:
                media_path = item.get("media_path")
                fp_source = media_path[0] if isinstance(media_path, list) else media_path
                fingerprint = file_fingerprint(Path(fp_source)) if fp_source else None
                record_published(item, fingerprint)
            except Exception:
                pass
            log_event({
                "level": "success",
                "item_id": item["id"],
                "instagram_media_id": media_id,
                "message": f"Published {item['media_type']} scheduled for {item['scheduled_at']}",
            })
        except (InstagramAPIError, ValueError, KeyError) as e:
            mark_failed(item, str(e))
            had_failure = True
            log_event({
                "level": "error",
                "item_id": item["id"],
                "message": f"Failed to publish item {item['id']} ({item['media_type']}, "
                            f"scheduled {item['scheduled_at']}): {e}",
            })
        finally:
            save_queue(items, QUEUE_PATH)

    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())
