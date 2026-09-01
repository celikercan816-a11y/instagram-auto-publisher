"""Weekly plan generation + daily queue-filling.

generate_weekly_plan() runs Sundays (see .github/workflows/weekly-plan.yml) and
lays out next week's 6 slots as *intentions* in weekly_content_plan.json --
day, time, content type, theme, planned media source, a caption idea note and
a hashtag strategy note. No media/caption is actually generated yet at this
point.

Reels are disabled for now (no video-generation service is connected, and the
user does not want a risky slideshow+music placeholder auto-enabled) -- every
slot is a plain "post" (IMAGE), converted from the original 4-post/2-reels
split. Re-enabling reels later just means adding "reels" entries back to
SLOT_TEMPLATE and restoring a _build_reels_item() path; the REELS media_type
support in src/image_generator.py and src/instagram_api.py is untouched.

ensure_queue_filled() runs daily (see .github/workflows/daily-content-fill.yml)
and turns the next unqueued plan slots into real content_queue.json entries
(generating an image and a caption/hashtag set, then running them through
content_quality.run_quality_control) until there are at least MIN_READY
pending items scheduled within the next HORIZON_DAYS days. It only ever calls
the free Hugging Face image provider (see src/image_generator.py) -- if that
provider's free monthly quota is exhausted, it stops trying for the rest of
this run and leaves the remaining slots as "planned" for tomorrow's run,
rather than falling back to any paid service.

DISABLED 2026-09-01 (legacy pipeline lockdown): this whole module is the
pre-pivot lifestyle/travel/style content system, superseded by the quote+
manzara pivot. It was found still running via the (now-disabled, see
.github/workflows/daily-content-fill.yml and weekly-plan.yml) daily/weekly
cron for days after the pivot, auto-queueing and even auto-publishing
fabricated-claim captions. Both public entry points below now refuse to run
unless ALLOW_LEGACY_PIPELINE=1 is explicitly set in the environment -- a
deliberate, inspectable opt-in, never a silent default -- so a re-enabled
workflow step, a stray manual invocation, or another script importing this
module can't accidentally revive it. Items this module produces are never
stamped pipeline_version="quote_v1", so queue_manager.get_due_items()'s hard
guard would refuse to publish them even if this guard were ever bypassed.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta

from src.config import Config
from src.content_bank import (
    compose_caption,
    generate_caption,
    generate_content_attributes,
    generate_hashtags,
    pick_shot_type_for_slot,
    pick_theme_for_slot,
    resolve_theme,
)
from src.content_history import last_n, load_history, recent
from src.content_quality import run_quality_control
from src.image_generator import QuotaExhaustedError, find_local_media, generate_image, generate_image_prompt
from src.queue_manager import add_item, load_queue, save_queue

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PLAN_PATH = PROJECT_ROOT / "weekly_content_plan.json"

MIN_READY = 3
HORIZON_DAYS = 7

# Shot types where a real person is meaningfully shown -- per the approved
# 2026-09-01 design these NEVER go through text-to-image "identity
# recreation" (tried and rejected -- see src/person_composite.py's
# docstring). ensure_queue_filled() (the automated/cron path) defers these
# slots entirely rather than generating them; only
# scripts/prepare_person_previews.py (run locally, where reference_photos/
# actually exists) builds them, and only into preview_pending/ for human
# approval -- never straight into content_queue.json.
PERSON_VISIBLE_SHOT_TYPES = {"face_visible", "distant_or_profile_or_back", "experimental_spontaneous"}

# (day offset from week start (Mon=0), "HH:MM" local time, content_type)
# Reels disabled for now -- all slots are "post". Kept spread across the week
# (not daily) per the "spam yapma" instruction.
SLOT_TEMPLATE = [
    (0, "12:00", "post"),
    (1, "19:30", "post"),
    (3, "19:30", "post"),
    (4, "12:00", "post"),
    (5, "19:30", "post"),
    (6, "18:00", "post"),
]

TZ_OFFSET = "+03:00"


def _load_plan() -> dict:
    if not PLAN_PATH.exists():
        return {"week_start": None, "week_end": None, "generated_at": None, "items": []}
    with open(PLAN_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _save_plan(plan: dict) -> None:
    with open(PLAN_PATH, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _legacy_pipeline_guard(fn_name: str) -> bool:
    """Returns True if the caller should proceed. See module docstring's
    "DISABLED 2026-09-01" note. Logs loudly either way so a silent skip is
    never mistaken for "nothing to do"."""
    if os.environ.get("ALLOW_LEGACY_PIPELINE") == "1":
        print(f"[content_planner] ALLOW_LEGACY_PIPELINE=1 set -- proceeding with legacy {fn_name}() despite the 2026-09-01 pivot lockdown.")
        return True
    print(f"[content_planner] LEGACY PIPELINE DISABLED -- {fn_name}() refused to run (set ALLOW_LEGACY_PIPELINE=1 to override). "
          "See module docstring: superseded by the quote+manzara pivot.")
    return False


def generate_weekly_plan(start_date: date | None = None) -> dict:
    """Builds next week's plan (Monday..Sunday) and overwrites
    weekly_content_plan.json. Does not touch content_queue.json --
    ensure_queue_filled() is what turns a slot into real content."""
    if not _legacy_pipeline_guard("generate_weekly_plan"):
        return _load_plan()
    today = start_date or date.today()
    # next Monday (if today already is Monday, still plan the week starting today)
    days_until_monday = (7 - today.weekday()) % 7
    week_start = today + timedelta(days=days_until_monday)
    week_end = week_start + timedelta(days=6)

    recent_themes: list[str] = []
    items = []
    for day_offset, time_str, content_type in SLOT_TEMPLATE:
        theme = pick_theme_for_slot(recent_themes)
        recent_themes.append(theme)
        day = week_start + timedelta(days=day_offset)
        items.append({
            "id": str(uuid.uuid4()),
            "day": day.isoformat(),
            "time": time_str,
            "content_type": content_type,
            "theme": theme,
            "media_source_plan": "local_if_available_else_ai",
            "caption_idea": f"'{theme}' temasında doğal, kısa bir caption",
            "hashtag_strategy": f"{theme} hashtag havuzundan 5-10 tanesi, son kullanılanla düşük örtüşme",
            "status": "planned",
            "queue_item_id": None,
        })

    plan = {
        "week_start": week_start.isoformat(),
        "week_end": week_end.isoformat(),
        "generated_at": datetime.now().isoformat(),
        "items": items,
    }
    _save_plan(plan)
    return plan


def _slot_datetime(slot: dict) -> datetime:
    return datetime.fromisoformat(f"{slot['day']}T{slot['time']}:00{TZ_OFFSET}")


def _build_post_item(slot: dict, config: Config, history: list[dict], theme: str | None = None, shot_type: str | None = None) -> dict:
    """theme/shot_type may be passed in already-resolved (ensure_queue_filled
    does this so it can decide PERSON_VISIBLE routing before generating
    anything); if omitted, resolved/picked here as before, for other callers
    (e.g. scripts/test_publish_today.py)."""
    theme = resolve_theme(theme if theme is not None else slot["theme"])
    item_id = slot["id"]

    media_path = find_local_media(theme)
    if media_path:
        # A real photo -- its actual pose/outfit/camera-angle aren't known to
        # this code, so only the theme is recorded (never fabricate
        # attributes for a real, unlabeled file).
        media_source = "local"
        image_prompt = None
        attributes = {"theme": theme}
        rel_path = str(media_path.relative_to(PROJECT_ROOT)).replace("\\", "/")
    else:
        if shot_type is None:
            recent_shot_types = [
                (e.get("attributes") or {}).get("shot_type")
                for e in last_n(history, 5)
                if (e.get("attributes") or {}).get("shot_type")
            ]
            shot_type = pick_shot_type_for_slot(recent_shot_types)
        attributes = generate_content_attributes(theme, shot_type, history)
        image_prompt = generate_image_prompt(theme, shot_type=shot_type, attributes=attributes)
        generated_path = generate_image(theme, item_id, is_reels=False, prompt=image_prompt)
        media_source = "ai_generated"
        rel_path = str(generated_path.relative_to(PROJECT_ROOT)).replace("\\", "/")

    media_url = config.media_public_url(rel_path)

    used_captions = {e.get("caption_summary", "") for e in history}
    recent_sets = [e.get("hashtags") or [] for e in recent(history, days=30) if e.get("theme") == theme]
    caption_text, caption_style = generate_caption(theme, used_captions, history=history, media_source=media_source)
    attributes["caption_style"] = caption_style
    hashtags = generate_hashtags(theme, recent_sets)
    caption = compose_caption(caption_text, hashtags)

    items = load_queue()
    item = add_item(
        items,
        media_type="IMAGE",
        media_url=media_url,
        caption=caption,
        scheduled_at=_slot_datetime(slot).isoformat(),
        allow_duplicate=True,  # uniqueness already enforced by content_quality below
        content_type="post",
        theme=theme,
        media_source=media_source,
        media_path=rel_path,
        image_prompt=image_prompt,
        hashtags=hashtags,
        item_id=item_id,
        attributes=attributes,
    )
    from src.content_history import file_fingerprint
    fingerprint = file_fingerprint(PROJECT_ROOT / rel_path)
    run_quality_control(item, history, media_fingerprint=fingerprint)
    save_queue(items)
    return item


def ensure_queue_filled(min_ready: int = MIN_READY, horizon_days: int = HORIZON_DAYS) -> dict:
    """Returns a small report dict describing what it did (for logging)."""
    if not _legacy_pipeline_guard("ensure_queue_filled"):
        return {"ready_before": 0, "ready_after": 0, "created": [], "needs_review": [],
                "quota_stopped": False, "deferred_person_visible": [],
                "note": "LEGACY PIPELINE DISABLED (see src/content_planner.py module docstring)"}
    config = Config.load(require_token=False)
    history = load_history()

    queue = load_queue()
    now = datetime.now().astimezone()
    horizon_end = now + timedelta(days=horizon_days)
    ready_count = sum(
        1 for i in queue
        if i.get("status") == "pending"
        and now <= datetime.fromisoformat(i["scheduled_at"]) <= horizon_end
    )

    report = {"ready_before": ready_count, "created": [], "needs_review": [], "quota_stopped": False, "deferred_person_visible": []}
    if ready_count >= min_ready:
        return report

    plan = _load_plan()
    if not plan.get("items"):
        report["note"] = "weekly_content_plan.json boş, önce generate_weekly_plan() çalışmalı"
        return report

    pending_slots = [
        s for s in plan["items"]
        if s["status"] == "planned" and _slot_datetime(s) <= horizon_end
    ]
    pending_slots.sort(key=_slot_datetime)

    for slot in pending_slots:
        if ready_count >= min_ready:
            break
        theme = resolve_theme(slot["theme"])
        recent_shot_types = [
            (e.get("attributes") or {}).get("shot_type")
            for e in last_n(history, 5)
            if (e.get("attributes") or {}).get("shot_type")
        ]
        shot_type = pick_shot_type_for_slot(recent_shot_types)
        if shot_type in PERSON_VISIBLE_SHOT_TYPES:
            # Never generated here (this path has no reference_photos/ access
            # in GitHub Actions, and even locally this content requires human
            # approval) -- left "planned" for scripts/prepare_person_previews.py
            # to pick up on a machine that actually has reference_photos/.
            report["deferred_person_visible"].append(slot["id"])
            continue
        try:
            item = _build_post_item(slot, config, history, theme=theme, shot_type=shot_type)
            slot["status"] = "queued"
            slot["queue_item_id"] = item["id"]
            if item["status"] == "pending":
                ready_count += 1
                report["created"].append(item["id"])
            else:
                report["needs_review"].append(item["id"])
        except QuotaExhaustedError as e:
            # Free quota is used up for this run -- stop entirely, leave this
            # and every remaining slot as "planned" so tomorrow's run retries
            # them. Never fall back to a paid provider.
            report["quota_stopped"] = True
            report.setdefault("errors", []).append(f"{slot['id']}: {e}")
            break
        except Exception as e:
            # Transient failure (network hiccup, etc.) -- leave the slot
            # "planned" so it's retried on the next run instead of being
            # permanently given up on.
            report.setdefault("errors", []).append(f"{slot['id']}: {e}")

    _save_plan(plan)
    report["ready_after"] = ready_count
    return report
