"""Daily FEED+STORY content generation for the Quote+Manzara pipeline
(pipeline_version="quote_v1", 2026-09-01 automation build).

Flow per point 1 of the build spec -- QUOTE FIRST, THEN IMAGE:
  approved quote pool -> pick one (theme/pattern/duplicate diversity checked
  against content_history.json) -> pick a mood-matching landscape scene
  (diversity checked) -> Cloudflare background (max 3 attempts, then
  needs_review -- never a paid fallback) -> local typography
  (text_renderer.render_quote_editorial for feed, render_quote_story for
  story) -> quote_quality.evaluate_quote_post QC -> content_queue.json
  (only for today's remaining slots) or content_reserve/ (everything else,
  for a future day to use without spending another Cloudflare call).

Honest scope note: this module isolates failures per-item (one bad item
never stops the rest of the run, mirroring src/publisher.py's existing
per-item isolation) and reads the reserve pool before generating anything
new, but it does NOT implement live same-day "swap in a reserve item the
moment a scheduled publish fails" -- a failed queue item is marked "failed"
and simply won't be retried (get_due_items() only ever considers
status=="pending"); the NEXT daily run reconciles by generating/pulling a
replacement for whatever's short. Building true same-day hot-swap is future
work, not silently claimed as done here.
"""
import json
import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from src.config import Config  # noqa: F401  (import side effect: triggers load_dotenv() before anything reads CLOUDFLARE_*/IG_* env vars)
from src.content_history import last_n, load_history, record_published
from src.image_generator import (
    CLOUDFLARE_MODEL, CloudflareConfigError, CloudflareQuotaExhaustedError,
    _call_cloudflare_image_api,
)
from src.quote_generator import ORIGINAL_QUOTE_ENGINE_VERSION, quote_hash
from src.quote_impact import check_pattern_repetition, check_semantic_duplicate
from src.quote_quality import evaluate_quote_post
from src.quote_scenes import LANDSCAPE_SCENES, build_scene_prompt, select_scene_for_mood
from src.queue_manager import PIPELINE_VERSION, add_item, load_queue, save_queue
from src.text_renderer import render_quote_editorial, render_quote_story

PROJECT_ROOT = Path(__file__).resolve().parent.parent
TZ = ZoneInfo("Europe/Istanbul")

APPROVED_POOL_PATH = PROJECT_ROOT / "data" / "approved_quotes_pool.json"
RESERVE_FEED_DIR = PROJECT_ROOT / "content_reserve" / "feed"
RESERVE_STORY_DIR = PROJECT_ROOT / "content_reserve" / "story"
PLAN_PATH = PROJECT_ROOT / "daily_publish_plan.json"
GENERATED_DIR = PROJECT_ROOT / "media" / "generated"
LOG_PATH = PROJECT_ROOT / "logs" / "daily_planner_log.jsonl"

# Point 5/6.
DAILY_FEED_MIN, DAILY_FEED_TARGET, DAILY_FEED_MAX = 8, 9, 10
DAILY_STORY_MIN, DAILY_STORY_TARGET, DAILY_STORY_MAX = 10, 15, 20
FEED_TIMES = ["08:30", "10:30", "12:30", "14:30", "16:30", "18:30", "20:30", "22:00", "23:30"]
# Point 12: 1-3 stories per natural block, not 15 back-to-back.
STORY_BLOCKS = [
    ("07:30", "10:30", 3), ("10:30", "14:00", 3), ("14:00", "18:00", 3),
    ("18:00", "22:00", 3), ("22:00", "23:59", 3),
]

MAX_BACKGROUND_ATTEMPTS = 3  # point 10: "Bir içerik için maksimum: 3 background attempt. Sonra: needs_review"
FEED_QUOTE_MIN, FEED_VISUAL_MIN, FEED_OVERALL_MIN = 85, 85, 88


def _log(event: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")
    print(json.dumps(event, ensure_ascii=False))


def load_approved_pool() -> list[dict]:
    if not APPROVED_POOL_PATH.exists():
        return []
    with open(APPROVED_POOL_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["quotes"]


def save_approved_pool(quotes: list[dict]) -> None:
    APPROVED_POOL_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(APPROVED_POOL_PATH, "w", encoding="utf-8") as f:
        json.dump({"engine_version": ORIGINAL_QUOTE_ENGINE_VERSION, "quotes": quotes}, f, ensure_ascii=False, indent=2)
        f.write("\n")


def _recent_quote_v1_attributes(history: list[dict], window: int = 20) -> list[dict]:
    return [e.get("attributes") or {} for e in last_n(history, window) if (e.get("attributes") or {}).get("pipeline_version") == PIPELINE_VERSION]


def pick_quote(pool: list[dict], used_ids: set[str], history: list[dict], recent_themes: list[str],
                session_texts: list[str] | None = None) -> dict | None:
    """Returns an unused quote dict from the approved pool respecting: not
    already used today, not a semantic/exact duplicate of recently
    published quotes, sentence-pattern repetition limit (point 3), and no
    identical theme as the immediately preceding pick (point 8).

    session_texts: quotes already picked EARLIER IN THIS SAME RUN. Real
    published history (content_history.json) only grows on an actual
    publish, so a single run picking e.g. 24 quotes back-to-back would
    otherwise never see its own earlier picks -- this closes that gap."""
    from src.quote_pool_manager import all_known_quote_texts  # local import: avoids a module-load-order dependency

    recent_attrs = _recent_quote_v1_attributes(history)
    recent_texts = (
        [a.get("quote_text", "") for a in recent_attrs if a.get("quote_text")]
        + list(session_texts or [])
        + list(all_known_quote_texts(include_pool=False))  # point 3: gold_quotes + reserve, not just published history -- include_pool=True here would make every candidate a "duplicate" of its own pool entry, see quote_pool_manager.all_known_quote_texts() docstring
    )
    candidates = [q for q in pool if q["id"] not in used_ids]
    candidates.sort(key=lambda q: -q["score"])  # prefer the strongest first, ties broken by diversity checks below
    for q in candidates:
        if recent_themes and q["theme"] == recent_themes[-1]:
            continue
        if check_pattern_repetition(q["text"], recent_texts, max_repeat=2, window=20):
            continue
        is_dup, _, _ = check_semantic_duplicate(q["text"], recent_texts)
        if is_dup:
            continue
        return q
    # relax the "no same theme as immediately previous" rule if that's the only blocker left
    for q in candidates:
        if check_pattern_repetition(q["text"], recent_texts, max_repeat=2, window=20):
            continue
        is_dup, _, _ = check_semantic_duplicate(q["text"], recent_texts)
        if is_dup:
            continue
        return q
    return None


def pick_scene(mood: str, recent_scene_ids: list[str]) -> dict:
    avoid = set(recent_scene_ids[-2:])  # point 9: no back-to-back repeat of the same scene
    return select_scene_for_mood(mood, avoid_ids=avoid)


def _generate_background(prompt: str, item_id: str) -> tuple:
    """Returns (PIL.Image, native_size, neurons_estimate) or (None, None, 0)
    after MAX_BACKGROUND_ATTEMPTS. Cloudflare only -- no paid fallback ever."""
    for attempt in range(1, MAX_BACKGROUND_ATTEMPTS + 1):
        try:
            t0 = time.monotonic()
            bg = _call_cloudflare_image_api(prompt, (1024, 1024))
            _log({"level": "success", "item_id": item_id, "attempt": attempt, "message": "Cloudflare background OK",
                  "duration_s": round(time.monotonic() - t0, 2)})
            return bg, bg.size, 43.2
        except CloudflareConfigError as e:
            _log({"level": "error", "item_id": item_id, "message": f"Cloudflare yapılandırma hatası: {e}"})
            return None, None, 0
        except CloudflareQuotaExhaustedError as e:
            _log({"level": "warning", "item_id": item_id, "attempt": attempt, "message": f"Kota/rate-limit: {e}"})
            if attempt < MAX_BACKGROUND_ATTEMPTS:
                time.sleep(30 * attempt)  # controlled backoff, point 10/18 -- never infinite retry
        except RuntimeError as e:
            _log({"level": "error", "item_id": item_id, "attempt": attempt, "message": f"Cloudflare hatası: {e}"})
            if attempt < MAX_BACKGROUND_ATTEMPTS:
                time.sleep(10)
    return None, None, 0


def generate_feed_content(quote: dict, scene: dict, item_id: str, recent_zones: list[str], history: list[dict]) -> dict:
    """Returns a result dict: {status: ok|needs_review|needs_generation, ...}."""
    prompt = build_scene_prompt(scene)
    bg, native_size, neurons = _generate_background(prompt, item_id)
    if bg is None:
        return {"status": "needs_generation", "reason": "Cloudflare background üretilemedi (3 deneme).", "neurons": 0}

    raw_path = GENERATED_DIR / f"{item_id}_feed_raw.jpg"
    raw_path.parent.mkdir(parents=True, exist_ok=True)
    bg.convert("RGB").save(raw_path, format="JPEG", quality=95)

    best = None
    for attempt in range(1, 4):
        r_zones = recent_zones if attempt == 1 else ([best["zone"], best["zone"]] if best else recent_zones)
        fscale = 1.0 if attempt < 3 else 0.92
        render = render_quote_editorial(bg, quote["text"], author=quote.get("author"), recent_zones=r_zones, font_scale=fscale)
        if not render.fit_ok:
            continue
        out_path = GENERATED_DIR / f"{item_id}_feed.jpg"
        render.image.save(out_path, format="JPEG", quality=92)
        qc = evaluate_quote_post(str(out_path), quote, render, history, "EDITORIAL", content_format="feed")
        qc_pass = (not qc["hard_fail"]) and qc["quality_score"] >= FEED_OVERALL_MIN and render.placement_score >= 0.6
        best = {"render": render, "qc": qc, "out_path": out_path, "zone": render.zone}
        if qc_pass:
            break

    if best is None:
        return {"status": "needs_review", "reason": "Metin hiçbir denemede kadraja sığmadı.", "neurons": neurons}
    qc = best["qc"]
    if qc["hard_fail"] or qc["quality_score"] < FEED_OVERALL_MIN or best["render"].placement_score < 0.6:
        return {"status": "needs_review", "reason": f"QC geçemedi: score={qc['quality_score']}, placement={best['render'].placement_score:.2f}",
                "qc": qc, "neurons": neurons}

    return {
        "status": "ok", "image_path": str(best["out_path"].relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "raw_path": str(raw_path.relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "zone": best["render"].zone, "quality_score": qc["quality_score"],
        "score_breakdown": qc["score_breakdown"], "neurons": neurons,
    }


def generate_story_content(quote: dict, scene: dict, item_id: str, recent_zones: list[str], history: list[dict],
                            shared_background=None) -> dict:
    """If shared_background is given (a PIL.Image), reuses it instead of a
    new Cloudflare call -- point 7: same quote can appear in feed+story with
    a different crop/typography without wasting a Cloudflare call."""
    neurons = 0
    if shared_background is not None:
        bg = shared_background
    else:
        prompt = build_scene_prompt(scene)
        bg, _, neurons = _generate_background(prompt, item_id)
        if bg is None:
            return {"status": "needs_generation", "reason": "Cloudflare background üretilemedi (3 deneme).", "neurons": 0}
        raw_path = GENERATED_DIR / f"{item_id}_story_raw.jpg"
        raw_path.parent.mkdir(parents=True, exist_ok=True)
        bg.convert("RGB").save(raw_path, format="JPEG", quality=95)

    best = None
    for attempt in range(1, 4):
        r_zones = recent_zones if attempt == 1 else ([best["zone"], best["zone"]] if best else recent_zones)
        fscale = 1.0 if attempt < 3 else 0.9
        render = render_quote_story(bg, quote["text"], author=quote.get("author"), recent_zones=r_zones, font_scale=fscale)
        if not render.fit_ok:
            continue
        out_path = GENERATED_DIR / f"{item_id}_story.jpg"
        render.image.save(out_path, format="JPEG", quality=92)
        qc = evaluate_quote_post(str(out_path), quote, render, history, "STORY", content_format="story")
        qc_pass = (not qc["hard_fail"]) and render.placement_score >= 0.6
        best = {"render": render, "qc": qc, "out_path": out_path, "zone": render.zone}
        if qc_pass:
            break

    if best is None:
        return {"status": "needs_review", "reason": "Metin hiçbir denemede kadraja/safe-zone'a sığmadı.", "neurons": neurons}
    qc = best["qc"]
    if qc["hard_fail"] or best["render"].placement_score < 0.6:
        return {"status": "needs_review", "reason": f"QC geçemedi: score={qc['quality_score']}, placement={best['render'].placement_score:.2f}",
                "qc": qc, "neurons": neurons}

    return {
        "status": "ok", "image_path": str(best["out_path"].relative_to(PROJECT_ROOT)).replace("\\", "/"),
        "zone": best["render"].zone, "quality_score": qc["quality_score"],
        "score_breakdown": qc["score_breakdown"], "neurons": neurons,
    }


# ---------------------------------------------------------------------------
# Scheduling -- point 12 (daily hours) + point 22 (never bulk-publish past
# slots; a slot whose time already passed today is SKIPPED_PAST_TIME, not
# force-published now).
# ---------------------------------------------------------------------------

def _slot_datetime_today(time_str: str, today: date) -> datetime:
    hh, mm = (int(x) for x in time_str.split(":"))
    return datetime(today.year, today.month, today.day, hh, mm, tzinfo=TZ)


def _todays_scheduled_count(queue: list[dict], media_type: str, today: date) -> int:
    """Point 16 (duplicate protection): how many real quote_v1 items are
    already sitting in the queue for today (pending or published), of this
    media type -- used to shrink how many MORE should be scheduled today if
    this run happens twice in one day (a manual re-trigger, a workflow
    re-run), without touching the PAST-time filtering (a separate concern:
    a slot already gone is gone whether or not it got filled)."""
    count = 0
    for item in queue:
        if item.get("pipeline_version") != PIPELINE_VERSION or item.get("media_type") != media_type:
            continue
        if item.get("status") not in ("pending", "published"):
            continue
        try:
            sched = datetime.fromisoformat(item["scheduled_at"])
        except (KeyError, ValueError):
            continue
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        if sched.astimezone(TZ).date() == today:
            count += 1
    return count


def _occupied_feed_times_today(queue: list[dict], today: date) -> set[str]:
    """HH:MM labels (Europe/Istanbul) that already have a pending/published
    quote_v1 IMAGE item scheduled today -- see compute_feed_slots()."""
    occupied = set()
    for item in queue:
        if item.get("pipeline_version") != PIPELINE_VERSION or item.get("media_type") != "IMAGE":
            continue
        if item.get("status") not in ("pending", "published"):
            continue
        try:
            sched = datetime.fromisoformat(item["scheduled_at"])
        except (KeyError, ValueError):
            continue
        if sched.tzinfo is None:
            sched = sched.replace(tzinfo=timezone.utc)
        sched_local = sched.astimezone(TZ)
        if sched_local.date() == today:
            occupied.add(sched_local.strftime("%H:%M"))
    return occupied


def compute_feed_slots(now: datetime, queue: list[dict] | None = None) -> tuple[list[str], list[str]]:
    """Returns (remaining_today, skipped_past_time) as "HH:MM" strings,
    Europe/Istanbul. Removes slots whose time has passed AND (bug found
    2026-09-04 -- a same-day re-run genuinely double-booked 3 identical
    HH:MM slots in production) any slot already occupied by a pending/
    published item today: the "cap how many MORE to schedule" count in
    run_daily_content_generation() only shrinks the TOTAL target, it does
    not know which specific clock-time labels were already used, so without
    this a second same-day run (manual re-trigger, retry, etc.) happily
    reused the same FEED_TIMES strings for brand-new items."""
    today = now.astimezone(TZ).date()
    occupied = _occupied_feed_times_today(queue or [], today)
    remaining, skipped = [], []
    for t in FEED_TIMES:
        if t in occupied:
            continue
        (remaining if _slot_datetime_today(t, today) > now else skipped).append(t)
    return remaining, skipped


def compute_story_slots(now: datetime, queue: list[dict] | None = None) -> tuple[list[datetime], list[str]]:
    """Returns (remaining_datetimes, skipped_block_labels). Spreads each
    remaining block's story count evenly across whatever's left of that
    block (point 12: natural 1-3-story bursts with gaps, not all at once)."""
    today = now.astimezone(TZ).date()
    remaining: list[datetime] = []
    skipped_blocks = []
    for start_s, end_s, count in STORY_BLOCKS:
        start_dt, end_dt = _slot_datetime_today(start_s, today), _slot_datetime_today(end_s, today)
        block_start = max(start_dt, now + timedelta(minutes=5))
        span = (end_dt - block_start).total_seconds()
        if span <= 0:
            skipped_blocks.append(f"{start_s}-{end_s}")
            continue
        for i in range(count):
            remaining.append(block_start + timedelta(seconds=span * (i + 0.5) / count))
    return remaining, skipped_blocks


# ---------------------------------------------------------------------------
# Reserve pool (point 11).
# ---------------------------------------------------------------------------

def _write_reserve(directory: Path, entry: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry['content_id']}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(entry, f, ensure_ascii=False, indent=2, default=str)
    return path


def _load_reserve(directory: Path) -> list[tuple[Path, dict]]:
    directory.mkdir(parents=True, exist_ok=True)
    out = []
    for p in sorted(directory.glob("*.json")):
        with open(p, "r", encoding="utf-8") as f:
            out.append((p, json.load(f)))
    return out


def promote_one_reserve_item(content_type: str) -> dict | None:
    """Point 4 (reserve swap, 2026-09-01): called from src/publisher.py when
    a scheduled slot's primary content fails to publish after retries.
    Pulls ONE item from content_reserve/{feed,story}/, deletes its source
    file BEFORE returning (so it can never be offered twice, even if this
    promotion attempt itself later fails), and returns an add_item()-ready
    kwargs dict scheduled for right now so it's picked up in the same
    publisher.py run. Returns None if no reserve item of this type exists --
    the caller marks the original slot "skipped" in that case."""
    directory = RESERVE_FEED_DIR if content_type == "feed" else RESERVE_STORY_DIR
    files = sorted(directory.glob("*.json"))
    if not files:
        return None
    path = files[0]
    with open(path, "r", encoding="utf-8") as f:
        entry = json.load(f)
    path.unlink(missing_ok=True)
    media_type = "IMAGE" if content_type == "feed" else "STORIES"
    return _queue_from_reserve_or_fresh_entry(entry, datetime.now(timezone.utc), media_type)


def _queue_from_reserve_or_fresh_entry(entry: dict, scheduled_at: datetime, media_type: str) -> dict:
    """Builds the content_queue.json item dict from a reserve/fresh-generation
    entry (see daily_publish_plan.json schema, point 15). Stories carry no
    caption (Instagram doesn't render one); feed captions NEVER repeat the
    on-image quote verbatim -- generate_quote_caption() picks a short
    complementary line/emoji/word or nothing at all, per the account's
    original caption rules."""
    from src.config import Config
    from src.quote_generator import generate_quote_caption, generate_quote_hashtags

    config = Config.load(require_token=False)
    media_url = config.media_public_url(entry["image_path"])
    if media_type == "IMAGE":
        caption_text, _style = generate_quote_caption(entry["quote"]["mood"])
        hashtags = generate_quote_hashtags()
        caption = (caption_text + "\n\n" + " ".join(hashtags)).strip() if hashtags else caption_text
    else:
        caption, hashtags = "", []

    return dict(
        media_type=media_type, media_url=media_url, caption=caption,
        scheduled_at=scheduled_at.isoformat(),
        content_type="story" if media_type == "STORIES" else "post",
        theme=entry["theme"], media_source="ai_generated", media_path=entry["image_path"],
        hashtags=hashtags, quality_score=entry["overall_score"], item_id=entry["content_id"],
        pipeline_version=PIPELINE_VERSION,
        attributes={
            "pipeline_version": PIPELINE_VERSION, "quote_hash": entry["quote"]["hash"],
            "quote_text": entry["quote"]["text"], "mood": entry["quote"]["mood"],
            "category": entry["quote"]["category"], "source_type": entry["source_type"],
            "theme": entry["theme"], "zone": entry.get("zone"),
        },
    )


def run_daily_content_generation() -> dict:
    """The main entry point (see scripts/generate_daily_quote_content.py).
    Fills today's remaining feed/story slots first (skipping any slot whose
    time already passed today), then tops up content_reserve/ for future
    days up to DAILY_FEED_TARGET/DAILY_STORY_TARGET. Never falls back to a
    paid image provider; never force-fills a slot with sub-threshold
    content (point 26: quality first, count second)."""
    now = datetime.now(timezone.utc)
    history = load_history()
    pool = load_approved_pool()
    if not pool:
        return {"status": "no_pool", "note": "data/approved_quotes_pool.json boş/yok -- önce onaylı söz havuzu oluşturulmalı."}

    queue = load_queue()
    used_ids: set[str] = set()
    recent_zones_feed: list[str] = []
    recent_zones_story: list[str] = []
    recent_themes: list[str] = []
    recent_scene_ids: list[str] = []
    session_texts: list[str] = []  # quotes picked earlier in THIS run -- see pick_quote()'s docstring

    feed_slots, feed_skipped = compute_feed_slots(now, queue)
    story_slots, story_skipped_blocks = compute_story_slots(now, queue)

    # Point 16 (duplicate protection): if today's target is already partly
    # (or fully) met by a previous run today, only aim for the remainder --
    # never re-derive a fresh slot list, which is what would double-book an
    # already-filled time.
    today_date = now.astimezone(TZ).date()
    feed_target_today = max(0, DAILY_FEED_TARGET - _todays_scheduled_count(queue, "IMAGE", today_date))
    story_target_today = max(0, DAILY_STORY_TARGET - _todays_scheduled_count(queue, "STORIES", today_date))

    report = {
        "date": now.astimezone(TZ).date().isoformat(), "generated_at": now.isoformat(),
        "feed_scheduled_today": [], "feed_reserved": [], "feed_skipped_past_time": feed_skipped,
        "story_scheduled_today": [], "story_reserved": [], "story_skipped_blocks": story_skipped_blocks,
        "needs_review": [], "total_neurons": 0.0, "errors": [],
    }

    reserve_feed = _load_reserve(RESERVE_FEED_DIR)
    reserve_story = _load_reserve(RESERVE_STORY_DIR)

    # ---- FEED: reserve first, then generate fresh up to DAILY_FEED_TARGET ----
    consecutive_cloudflare_failures = 0
    feed_entries: list[tuple[str, dict, Path | None]] = [("reserve", e, p) for p, e in reserve_feed[:DAILY_FEED_TARGET]]
    while len(feed_entries) < DAILY_FEED_TARGET:
        quote = pick_quote(pool, used_ids, history, recent_themes, session_texts)
        if quote is None:
            report["errors"].append("Yeterli çeşitlilikte söz kalmadı (pattern/tema/duplicate kısıtı) -- feed hedefi tam doldurulamadı.")
            break
        used_ids.add(quote["id"])
        recent_themes.append(quote["theme"])
        session_texts.append(quote["text"])
        scene = pick_scene(quote["mood"], recent_scene_ids)
        recent_scene_ids.append(scene["id"])
        item_id = str(uuid.uuid4())
        result = generate_feed_content(quote, scene, item_id, recent_zones_feed, history)
        report["total_neurons"] += result.get("neurons", 0)
        if result["status"] != "ok":
            report["needs_review"].append({"content_id": item_id, "type": "feed", "quote": quote["text"], "reason": result.get("reason")})
            if result["status"] == "needs_generation":
                consecutive_cloudflare_failures += 1
                if consecutive_cloudflare_failures >= 3:
                    report["errors"].append("3 ardışık Cloudflare hatası -- muhtemelen kota/servis sorunu, feed üretimi durduruldu (ücretli servise geçilmedi).")
                    break
            else:
                consecutive_cloudflare_failures = 0
            continue
        consecutive_cloudflare_failures = 0
        recent_zones_feed.append(result["zone"])
        entry = {
            "content_id": item_id, "type": "feed", "quote": quote, "source_type": quote["category"],
            "theme": quote["theme"], "image_path": result["image_path"], "quote_score": quote["score"],
            "visual_score": result["quality_score"], "overall_score": result["quality_score"],
            "zone": result["zone"], "generated_at": now.isoformat(),
        }
        feed_entries.append(("fresh", entry, None))

    feed_slots_usable = min(len(feed_slots), feed_target_today)
    for i, entry_tuple in enumerate(feed_entries):
        source, entry, reserve_path = entry_tuple
        if i < feed_slots_usable:
            scheduled_at = _slot_datetime_today(feed_slots[i], now.astimezone(TZ).date())
            queue_item = _queue_from_reserve_or_fresh_entry(entry, scheduled_at, "IMAGE")
            add_item(queue, **queue_item)
            report["feed_scheduled_today"].append({"content_id": entry["content_id"], "scheduled_at": scheduled_at.isoformat(), "theme": entry["theme"]})
            if reserve_path:
                reserve_path.unlink(missing_ok=True)
        else:
            if source == "fresh":
                _write_reserve(RESERVE_FEED_DIR, entry)
            report["feed_reserved"].append(entry["content_id"])

    # ---- STORY: reserve first, then generate fresh up to DAILY_STORY_TARGET ----
    consecutive_cloudflare_failures = 0
    story_entries: list[tuple[str, dict, Path | None]] = [("reserve", e, p) for p, e in reserve_story[:DAILY_STORY_TARGET]]
    while len(story_entries) < DAILY_STORY_TARGET:
        quote = pick_quote(pool, used_ids, history, recent_themes, session_texts)
        if quote is None:
            report["errors"].append("Yeterli çeşitlilikte söz kalmadı (pattern/tema/duplicate kısıtı) -- story hedefi tam doldurulamadı.")
            break
        used_ids.add(quote["id"])
        recent_themes.append(quote["theme"])
        session_texts.append(quote["text"])
        scene = pick_scene(quote["mood"], recent_scene_ids)
        recent_scene_ids.append(scene["id"])
        item_id = str(uuid.uuid4())
        result = generate_story_content(quote, scene, item_id, recent_zones_story, history)
        report["total_neurons"] += result.get("neurons", 0)
        if result["status"] != "ok":
            report["needs_review"].append({"content_id": item_id, "type": "story", "quote": quote["text"], "reason": result.get("reason")})
            if result["status"] == "needs_generation":
                consecutive_cloudflare_failures += 1
                if consecutive_cloudflare_failures >= 3:
                    report["errors"].append("3 ardışık Cloudflare hatası -- muhtemelen kota/servis sorunu, story üretimi durduruldu (ücretli servise geçilmedi).")
                    break
            else:
                consecutive_cloudflare_failures = 0
            continue
        consecutive_cloudflare_failures = 0
        recent_zones_story.append(result["zone"])
        entry = {
            "content_id": item_id, "type": "story", "quote": quote, "source_type": quote["category"],
            "theme": quote["theme"], "image_path": result["image_path"], "quote_score": quote["score"],
            "visual_score": result["quality_score"], "overall_score": result["quality_score"],
            "zone": result["zone"], "generated_at": now.isoformat(),
        }
        story_entries.append(("fresh", entry, None))

    story_slots_usable = min(len(story_slots), story_target_today)
    for i, entry_tuple in enumerate(story_entries):
        source, entry, reserve_path = entry_tuple
        if i < story_slots_usable:
            queue_item = _queue_from_reserve_or_fresh_entry(entry, story_slots[i], "STORIES")
            add_item(queue, **queue_item)
            report["story_scheduled_today"].append({"content_id": entry["content_id"], "scheduled_at": story_slots[i].isoformat(), "theme": entry["theme"]})
            if reserve_path:
                reserve_path.unlink(missing_ok=True)
        else:
            if source == "fresh":
                _write_reserve(RESERVE_STORY_DIR, entry)
            report["story_reserved"].append(entry["content_id"])

    save_queue(queue)

    plan = {
        "date": report["date"], "generated_at": report["generated_at"],
        "feed": report["feed_scheduled_today"], "story": report["story_scheduled_today"],
        "feed_skipped_past_time": feed_skipped, "story_skipped_blocks": story_skipped_blocks,
        "feed_reserved": report["feed_reserved"], "story_reserved": report["story_reserved"],
        "needs_review": report["needs_review"],
    }
    with open(PLAN_PATH, "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False, indent=2, default=str)

    report["status"] = "ok"
    return report
