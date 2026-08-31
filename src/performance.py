"""Instagram Insights fetching + light strategy learning.

update_history_with_performance() pulls engagement data for recently
published posts (Meta needs the post to "mature" a bit before insights are
meaningful, so only items published >24h ago are queried) and stores it back
on the matching content_history.json entry.

analyze_strategy() looks for a real signal across *several* posts (never
reacts to a single data point, per the user's explicit instruction) and
writes strategy_weights.json -- theme/hour/content-type multipliers that
content_planner.py can optionally read to bias future slot selection. Wiring
those weights into pick_theme_for_slot() is intentionally left as a manual
follow-up once there's enough real data to trust; this module only computes
and records the signal for now.
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

from src.config import Config
from src.content_history import load_history, save_history
from src.instagram_api import GRAPH_BASE
from src.queue_manager import QUEUE_PATH, load_queue, save_queue

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEIGHTS_PATH = PROJECT_ROOT / "strategy_weights.json"
MIN_SAMPLES_PER_GROUP = 5

# Current (2026) Graph API media-insights metrics for feed posts. If Meta has
# changed these again, the 400 response body (logged verbatim) will name the
# valid set -- update this list accordingly rather than guessing.
FEED_METRICS = ["reach", "likes", "comments", "saved", "shares", "total_interactions"]
REELS_METRICS = ["reach", "likes", "comments", "saved", "shares", "plays", "total_interactions"]


def fetch_media_insights(media_id: str, access_token: str, is_reels: bool) -> dict | None:
    metrics = REELS_METRICS if is_reels else FEED_METRICS
    resp = requests.get(
        f"{GRAPH_BASE}/{media_id}/insights",
        params={"metric": ",".join(metrics), "access_token": access_token},
        timeout=30,
    )
    if resp.status_code >= 400:
        print(f"[performance] insights fetch failed for {media_id} (HTTP {resp.status_code}): {resp.text[:300]}")
        return None
    out = {}
    for entry in resp.json().get("data", []):
        values = entry.get("values", [])
        out[entry["name"]] = values[0]["value"] if values else None
    return out


def _engagement_score(insights: dict) -> float:
    reach = insights.get("reach") or 0
    interactions = (
        insights.get("total_interactions")
        or sum(v or 0 for k, v in insights.items() if k in ("likes", "comments", "saved", "shares"))
    )
    if not reach:
        return 0.0
    return round(min(100.0, (interactions / reach) * 1000), 1)


def update_history_with_performance() -> int:
    config = Config.load(require_token=True)
    history = load_history()
    queue = load_queue()
    updated = 0
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    for entry in history:
        if entry.get("insights") is not None or not entry.get("instagram_media_id"):
            continue
        published_at = entry.get("published_at")
        if not published_at:
            continue
        dt = datetime.fromisoformat(published_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        if dt > cutoff:
            continue

        is_reels = entry.get("content_type") == "reels"
        insights = fetch_media_insights(entry["instagram_media_id"], config.access_token, is_reels)
        if insights is None:
            continue

        score = _engagement_score(insights)
        entry["insights"] = insights
        entry["performance_score"] = score
        updated += 1

        for q_item in queue:
            if q_item.get("id") == entry.get("id"):
                q_item["performance_score"] = score

    if updated:
        save_history(history)
        save_queue(queue, QUEUE_PATH)
    return updated


def analyze_strategy(min_samples: int = MIN_SAMPLES_PER_GROUP) -> dict:
    history = [e for e in load_history() if e.get("performance_score") is not None]

    def grouped_avg(key_fn):
        groups: dict = {}
        for e in history:
            k = key_fn(e)
            groups.setdefault(k, []).append(e["performance_score"])
        return {k: {"avg_score": round(sum(v) / len(v), 1), "n": len(v)}
                for k, v in groups.items() if len(v) >= min_samples}

    weights = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sample_size": len(history),
        "by_theme": grouped_avg(lambda e: e.get("theme")),
        "by_content_type": grouped_avg(lambda e: e.get("content_type")),
        "by_hour": grouped_avg(lambda e: datetime.fromisoformat(e["published_at"]).hour if e.get("published_at") else None),
    }
    with open(WEIGHTS_PATH, "w", encoding="utf-8") as f:
        json.dump(weights, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return weights
