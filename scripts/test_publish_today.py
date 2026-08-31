"""One-off, user-triggered end-to-end test: publish every not-yet-published
slot of the CURRENT weekly_content_plan.json for real, today.

Split into two phases per slot (driven interactively, see the conversation
this was built in) rather than one unattended loop, because the automated
pipeline has one known gap: there is no automated vision QC in the free-only
setup (see src/image_generator.py docstring), so an AI image can pass the
structural quality gate (resolution/aspect/corruption) while still containing
a defect only a human/vision reviewer would catch (e.g. fake handwriting on a
notebook page, seen during this exact test run). So:

  prepare_slot(slot)          -> generates/reuses image + caption/hashtags,
                                  runs the structural quality gate, saves the
                                  queue item as "pending" (NOT published yet).
  publish_prepared_item(item) -> commits+pushes the media to GitHub (so
                                  raw.githubusercontent.com can actually serve
                                  it -- a real bug hit on the first run:
                                  Instagram can't fetch media that was only
                                  generated locally and never pushed), waits
                                  for it to be fetchable, then really
                                  publishes and verifies via the Graph API.

prepare_slot() also reuses an already-generated-but-not-yet-published item for
the same slot id if one exists on disk, so a retry after a bug fix (or a
manual rejection) doesn't burn more of the free HF quota than necessary.

This is not part of the regular cron pipeline (publish.yml / daily-content-
fill.yml / weekly-plan.yml are untouched and keep running as before).
"""
import json
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.config import Config
from src.content_history import file_fingerprint, load_history, record_published, recent
from src.content_bank import generate_caption, generate_hashtags, generate_image_prompt
from src.content_planner import PROJECT_ROOT, _load_plan, _save_plan, _slot_datetime
from src.content_quality import run_quality_control
from src.image_generator import QuotaExhaustedError, find_local_media, generate_image
from src.instagram_api import GRAPH_BASE, InstagramAPIError, InstagramClient
from src.publisher import publish_item
from src.queue_manager import add_item, load_queue, mark_failed, mark_published, save_queue

TEST_LOG_PATH = PROJECT_ROOT / "logs" / "test_publish_log.jsonl"


def log(event: dict) -> None:
    TEST_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    line = json.dumps(event, ensure_ascii=False)
    with open(TEST_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")
    print(line, flush=True)


def _existing_item_for_slot(slot_id: str) -> dict | None:
    for item in load_queue():
        if item.get("id") == slot_id and item.get("status") in ("pending", "failed"):
            path = item.get("media_path")
            path = path[0] if isinstance(path, list) else path
            if path and (PROJECT_ROOT / path).exists():
                return item
    return None


def make_placeholder(slot: dict) -> dict:
    theme = slot["theme"]
    caption_text = generate_caption(theme, set())
    hashtags = generate_hashtags(theme, [])
    caption = caption_text + "\n\n" + " ".join(hashtags)
    items = load_queue()
    item = add_item(
        items, media_type="IMAGE", media_url=None, caption=caption,
        scheduled_at=datetime.now(timezone.utc).isoformat(), allow_duplicate=True,
        content_type="post", theme=theme, media_source=None, media_path=None,
        hashtags=hashtags, status="needs_generation", item_id=slot["id"],
    )
    save_queue(items)
    return item


def prepare_slot(slot: dict, config: Config, history: list[dict], force_regenerate: bool = False) -> dict:
    """Returns the queue item dict. item['status'] is 'pending' if it cleared
    QC, 'needs_review' if not. Raises QuotaExhaustedError if generation was
    needed and the free quota is exhausted."""
    theme = slot["theme"]
    item_id = slot["id"]

    if not force_regenerate:
        existing = _existing_item_for_slot(item_id)
        if existing:
            log({"level": "info", "message": f"Slot {item_id}: diskteki mevcut görsel yeniden kullanılıyor.",
                 "media_path": existing.get("media_path")})
            return existing

    media_path = find_local_media(theme)
    if media_path:
        media_source, image_prompt = "local", None
        rel_path = str(media_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    else:
        image_prompt = generate_image_prompt(theme)
        generated = generate_image(theme, item_id, is_reels=False, prompt=image_prompt)
        media_source = "ai_generated"
        rel_path = str(generated.relative_to(PROJECT_ROOT)).replace("\\", "/")
    media_url = config.media_public_url(rel_path)

    used_captions = {e.get("caption_summary", "") for e in history}
    recent_sets = [e.get("hashtags") or [] for e in recent(history, days=30) if e.get("theme") == theme]
    caption_text = generate_caption(theme, used_captions)
    hashtags = generate_hashtags(theme, recent_sets)
    caption = caption_text + "\n\n" + " ".join(hashtags)

    items = load_queue()
    items = [i for i in items if i.get("id") != item_id]  # drop a previous failed/rejected attempt for this slot
    item = add_item(
        items, media_type="IMAGE", media_url=media_url, caption=caption,
        scheduled_at=datetime.now(timezone.utc).isoformat(), allow_duplicate=True,
        content_type="post", theme=theme, media_source=media_source, media_path=rel_path,
        image_prompt=image_prompt, hashtags=hashtags, item_id=item_id,
    )
    fingerprint = file_fingerprint(PROJECT_ROOT / rel_path)
    run_quality_control(item, history, media_fingerprint=fingerprint)
    save_queue(items)
    log({"level": "info", "message": f"Slot {item_id} hazırlandı.", "theme": theme,
         "media_path": rel_path, "media_source": media_source,
         "quality_score": item["quality_score"], "status": item["status"]})
    return item


def push_media_to_github(rel_path, timeout_s: int = 60) -> None:
    """Commits+pushes the given repo-relative media path(s) and waits until
    raw.githubusercontent.com actually serves them (there's a short
    propagation delay right after a push)."""
    paths = rel_path if isinstance(rel_path, list) else [rel_path]
    config = Config.load(require_token=False)

    import os
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    branch = os.environ.get("GH_BRANCH", "master")

    subprocess.run(["git", "add", *paths], cwd=PROJECT_ROOT, check=True, env=git_env)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT, env=git_env)
    if diff.returncode != 0:  # there is something staged
        subprocess.run(["git", "commit", "-q", "-m", "test: media for today's live test publish"],
                        cwd=PROJECT_ROOT, check=True, env=git_env)
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], cwd=PROJECT_ROOT, check=True, env=git_env)
        subprocess.run(["git", "push", "-q", "origin", branch], cwd=PROJECT_ROOT, check=True, env=git_env)

    deadline = time.time() + timeout_s
    for path in paths:
        url = config.media_public_url(path)
        while time.time() < deadline:
            resp = requests.head(url, timeout=15)
            if resp.status_code == 200:
                break
            time.sleep(3)
        else:
            raise RuntimeError(f"{url} {timeout_s}s içinde erişilebilir olmadı")


def commit_state(message: str) -> None:
    """Commits+pushes the queue/plan/history/log state files. Called after
    every slot so progress survives even if the job is interrupted or a
    later slot fails."""
    import os
    git_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    branch = os.environ.get("GH_BRANCH", "master")
    paths = ["content_queue.json", "content_history.json", "weekly_content_plan.json",
             "logs/test_publish_log.jsonl", "logs/image_generation_log.jsonl"]
    subprocess.run(["git", "add", "--ignore-errors", *paths], cwd=PROJECT_ROOT, env=git_env)
    diff = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=PROJECT_ROOT, env=git_env)
    if diff.returncode != 0:
        subprocess.run(["git", "commit", "-q", "-m", message], cwd=PROJECT_ROOT, check=True, env=git_env)
        subprocess.run(["git", "pull", "--rebase", "-q", "origin", branch], cwd=PROJECT_ROOT, check=True, env=git_env)
        subprocess.run(["git", "push", "-q", "origin", branch], cwd=PROJECT_ROOT, check=True, env=git_env)


def verify_media(config: Config, media_id: str) -> dict | None:
    resp = requests.get(
        f"{GRAPH_BASE}/{media_id}",
        params={"fields": "id,media_type,permalink,timestamp", "access_token": config.access_token},
        timeout=30,
    )
    return resp.json() if resp.status_code < 400 else None


def publish_prepared_item(item: dict, slot: dict, config: Config, client: InstagramClient,
                           history: list[dict]) -> dict:
    push_media_to_github(item["media_path"])

    try:
        media_id = publish_item(client, item)
    except InstagramAPIError as e:
        mark_failed(item, str(e))
        items2 = load_queue()
        for i2 in items2:
            if i2["id"] == item["id"]:
                i2.update(item)
        save_queue(items2)
        slot["status"] = "failed"
        return {"result": "BAŞARISIZ", "theme": item["theme"], "quality_score": item["quality_score"],
                "reason": str(e)}

    mark_published(item, media_id)
    items2 = load_queue()
    for i2 in items2:
        if i2["id"] == item["id"]:
            i2.update(item)
    save_queue(items2)
    fingerprint = file_fingerprint(PROJECT_ROOT / item["media_path"])
    record_published(item, fingerprint)
    history.append({
        "id": item["id"], "theme": item["theme"], "content_type": "post",
        "caption_summary": item["caption"][:120], "hashtags": item["hashtags"],
        "image_fingerprint": fingerprint, "published_at": item["published_at"],
        "instagram_media_id": media_id, "insights": None, "performance_score": None,
    })

    verified = verify_media(config, media_id)
    slot["status"] = "published"
    slot["queue_item_id"] = item["id"]
    _save_plan_safe()

    return {
        "result": "BAŞARILI", "theme": item["theme"], "quality_score": item["quality_score"],
        "instagram_media_id": media_id, "permalink": (verified or {}).get("permalink"),
        "verified": verified is not None, "caption": item["caption"],
        "media_path": item["media_path"], "media_source": item["media_source"],
        "published_at": item["published_at"],
    }


_PLAN_CACHE = {}


def _save_plan_safe():
    if _PLAN_CACHE.get("plan") is not None:
        _save_plan(_PLAN_CACHE["plan"])


def get_plan() -> dict:
    if "plan" not in _PLAN_CACHE:
        _PLAN_CACHE["plan"] = _load_plan()
    return _PLAN_CACHE["plan"]


def pending_slots() -> list[dict]:
    plan = get_plan()
    slots = [s for s in plan["items"] if s["status"] == "planned"]
    slots.sort(key=_slot_datetime)
    return slots


def main() -> int:
    """Cloud-runnable entry point (see .github/workflows/test-publish-remaining.yml):
    publishes every still-"planned" slot of the current week, spaced
    GAP_SECONDS apart, verifying each one before moving on. Never falls back
    to a paid image provider if the free HF quota runs out mid-run -- the
    remaining slots are left as needs_generation for the normal daily job to
    pick up once quota resets."""
    import os

    gap_seconds = int(os.environ.get("TEST_PUBLISH_GAP_SECONDS", "420"))
    config = Config.load(require_token=True)
    client = InstagramClient(config)
    history = load_history()

    slots = pending_slots()
    log({"level": "info", "message": f"{len(slots)} planlanmış slot bulundu (cloud run)."})

    for idx, slot in enumerate(slots):
        is_last = idx == len(slots) - 1
        try:
            item = prepare_slot(slot, config, history)
        except QuotaExhaustedError as e:
            make_placeholder(slot)
            slot["status"] = "needs_generation"
            _save_plan_safe()
            log({"level": "warning", "slot_id": slot["id"], "message": f"Kota tükendi, kalanlar needs_generation: {e}"})
            commit_state(f"test: slot {slot['id']} needs_generation (kota tükendi)")
            break

        if item["status"] != "pending":
            slot["status"] = "needs_review"
            _save_plan_safe()
            log({"level": "warning", "slot_id": slot["id"], "message": "Kalite kontrolünden geçemedi, atlandı."})
            commit_state(f"test: slot {slot['id']} needs_review (kalite < 70)")
            if not is_last:
                time.sleep(10)
            continue

        result = publish_prepared_item(item, slot, config, client, history)
        log({"level": "info", "slot_id": slot["id"], **result})
        commit_state(f"test: slot {slot['id']} -> {result['result']}")

        if not is_last:
            time.sleep(gap_seconds)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
