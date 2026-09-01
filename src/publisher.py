"""Entry point run on a schedule (GitHub Actions) to publish anything due in content_queue.json.

Exits with a non-zero status if anything failed, so the GitHub Actions run shows
as failed and GitHub's default email notification tells the repo owner something
went wrong (see the "error" field on the queue item and logs/publish_log.jsonl
for the exact reason).

RESERVE SWAP (point 4, 2026-09-01 post-launch fix): primary content -> a
short controlled retry (transient errors only) -> if still failing, promote
one content_reserve/ item of the same media type and publish THAT instead
-> if no reserve item exists either, the original slot is marked "skipped"
(distinct from "failed" -- "failed" means an attempt errored, "skipped"
means nothing could be published for that slot at all). The reserve
promotion is a one-shot: it does not itself retry or chain to a second
reserve item, so one bad day can't spiral into publishing everything in
reserve. Never publishes the same reserve item twice -- promote_one_
reserve_item() deletes its source file before anything is attempted.
"""
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from src.config import Config, ConfigError
from src.content_history import file_fingerprint, record_published
from src.daily_planner import promote_one_reserve_item
from src.instagram_api import InstagramAPIError, InstagramClient
from src.queue_manager import QUEUE_PATH, add_item, get_due_items, load_queue, mark_failed, mark_published, save_queue

PUBLISH_RETRY_ATTEMPTS = 2
PUBLISH_RETRY_BACKOFF_S = 15

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

        published, last_error = False, None
        for attempt in range(1, PUBLISH_RETRY_ATTEMPTS + 1):
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
                published = True
                break
            except (InstagramAPIError, ValueError, KeyError) as e:
                last_error = str(e)
                log_event({
                    "level": "warning" if attempt < PUBLISH_RETRY_ATTEMPTS else "error",
                    "item_id": item["id"], "attempt": attempt,
                    "message": f"Publish attempt {attempt}/{PUBLISH_RETRY_ATTEMPTS} failed for "
                               f"{item['id']} ({item['media_type']}, scheduled {item['scheduled_at']}): {e}",
                })
                if isinstance(e, ValueError):
                    break  # structural/programming error -- retrying the same bad item won't help
                if attempt < PUBLISH_RETRY_ATTEMPTS:
                    time.sleep(PUBLISH_RETRY_BACKOFF_S)

        if not published:
            mark_failed(item, last_error or "unknown error")
            had_failure = True

            # A promoted reserve item that itself fails does NOT get a second
            # swap -- "one-shot" (see module docstring): without this guard,
            # a systemic outage (e.g. Instagram itself down) could chain
            # through the entire reserve pool in a single run.
            if item.get("_is_reserve_substitute"):
                item["status"] = "skipped"
                log_event({"level": "warning", "item_id": item["id"],
                           "message": "Reserve substitute also failed -- SKIPPED, no further swap (one-shot)."})
            else:
                reserve_type = "feed" if item["media_type"] == "IMAGE" else ("story" if item["media_type"] == "STORIES" else None)
                reserve_item = promote_one_reserve_item(reserve_type) if reserve_type else None
                if reserve_item is None:
                    item["status"] = "skipped"
                    log_event({"level": "warning", "item_id": item["id"],
                               "message": "No reserve candidate available -- slot SKIPPED, nothing published in its place."})
                else:
                    new_queue_item = add_item(items, **reserve_item)
                    new_queue_item["_is_reserve_substitute"] = True
                    log_event({"level": "info", "item_id": item["id"], "reserve_content_id": new_queue_item["id"],
                               "message": f"Primary item failed -- promoted reserve item {new_queue_item['id']} "
                                          f"({reserve_type}) into the queue, scheduled now, to be picked up this same run."})
                    due.append(new_queue_item)  # same dict object as in `items` -- safe to append while iterating `due`

        save_queue(items, QUEUE_PATH)

    return 1 if had_failure else 0


if __name__ == "__main__":
    sys.exit(main())
