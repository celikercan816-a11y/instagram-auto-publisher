"""Quote pool auto-replenishment (point 2, 2026-09-01 post-launch fixes).

Honest architecture note: this project's quote engine is deliberately NOT
backed by a live LLM call -- it's an offline, rule-based bank (see
src/quote_generator.py's docstring), because QUOTE_QUALITY_SCORE's most
important dimensions (emotional_impact, original_insight, depth,
memorability, relatability) are literary/editorial judgment calls that
can't be honestly computed by a formula (see src/quote_impact.py's
docstring). There is therefore no way for an unattended GitHub Actions run
to WRITE new quotes from scratch. "Auto-replenish" here means: when
data/approved_quotes_pool.json drops below QUOTE_POOL_LOW_WATERMARK, pull
more from data/quote_seed_bank.json -- a larger pre-authored, pre-scored
reserve built the same way as the live pool (same ORIGINAL_QUOTE_ENGINE_
VERSION rubric, same >=85 floor) -- re-running the SAME dedup/pattern
checks before admitting anything. If the seed bank itself runs low, this
logs that plainly (needs a new authoring round) rather than fabricating
quotes or lowering the bar to force a fill.
"""
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

from src.quote_generator import ORIGINAL_QUOTE_ENGINE_VERSION, quote_hash
from src.quote_impact import check_pattern_repetition, check_semantic_duplicate, detect_sentence_patterns

PROJECT_ROOT = Path(__file__).resolve().parent.parent
POOL_PATH = PROJECT_ROOT / "data" / "approved_quotes_pool.json"
SEED_BANK_PATH = PROJECT_ROOT / "data" / "quote_seed_bank.json"
GOLD_PATH = PROJECT_ROOT / "data" / "gold_quotes.json"
HISTORY_PATH = PROJECT_ROOT / "content_history.json"

QUOTE_POOL_LOW_WATERMARK = 50
QUOTE_POOL_TARGET = 150
MIN_ADMIT_SCORE = 85  # never admit below this, even from a pre-scored seed bank


def _load_json_list(path: Path, key: str) -> list:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f).get(key, [])


def all_known_quote_texts(include_pool: bool = True) -> set[str]:
    """Point 3 dedup surface: gold_quotes + published history + reserve +
    (optionally) the current live pool + today's daily_publish_plan.json
    (its schedule entries only carry a content_id/theme, but its
    needs_review entries do carry the quote text).

    include_pool=False for daily_planner.pick_quote()'s call site: every
    pick_quote() candidate IS a member of the live pool by construction, so
    including the pool here made check_semantic_duplicate() compare each
    candidate against its own exact text (ratio=1.0) and reject it as a
    "duplicate of itself" -- a real bug found 2026-09-04 that made pick_quote()
    return None on its very first call, every single run, regardless of pool
    size or diversity (the actual cause of "yeterli çeşitlilikte söz kalmadı"
    on 2026-09-02/03, not pool exhaustion as first assumed). check_and_
    replenish_pool() below still needs include_pool=True (its default) --
    it genuinely must know what's already IN the pool to avoid re-adding a
    seed-bank quote that's already there."""
    texts = {q["text"] for q in _load_json_list(GOLD_PATH, "quotes")}
    if include_pool:
        texts |= {q["text"] for q in _load_json_list(POOL_PATH, "quotes")}
    if HISTORY_PATH.exists():
        with open(HISTORY_PATH, "r", encoding="utf-8") as f:
            history = json.load(f)
        for e in history:
            t = (e.get("attributes") or {}).get("quote_text")
            if t:
                texts.add(t)
    for sub in ("feed", "story"):
        for p in (PROJECT_ROOT / "content_reserve" / sub).glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    entry = json.load(f)
                t = (entry.get("quote") or {}).get("text")
                if t:
                    texts.add(t)
            except (json.JSONDecodeError, OSError):
                continue
    plan_path = PROJECT_ROOT / "daily_publish_plan.json"
    if plan_path.exists():
        try:
            with open(plan_path, "r", encoding="utf-8") as f:
                plan = json.load(f)
            for r in plan.get("needs_review", []):
                if r.get("quote"):
                    texts.add(r["quote"])
        except (json.JSONDecodeError, OSError):
            pass
    return texts


def check_and_replenish_pool() -> dict:
    """Returns a report dict; never raises. Safe to call every daily run --
    a no-op (report['note'] explains why) when the pool is already healthy."""
    if POOL_PATH.exists():
        with open(POOL_PATH, "r", encoding="utf-8") as f:
            pool_data = json.load(f)
    else:
        pool_data = {"engine_version": ORIGINAL_QUOTE_ENGINE_VERSION, "quotes": []}
    pool = pool_data.get("quotes", [])
    report = {"pool_before": len(pool), "added": 0, "pool_after": len(pool),
              "seed_bank_remaining": None, "note": None}

    if len(pool) >= QUOTE_POOL_LOW_WATERMARK:
        report["note"] = f"Havuz sağlıklı ({len(pool)} >= watermark {QUOTE_POOL_LOW_WATERMARK}) -- replenish gerekmedi."
        return report

    if not SEED_BANK_PATH.exists():
        report["note"] = "data/quote_seed_bank.json yok -- yeni bir yazım turu (insan/Claude oturumu) gerekiyor."
        return report

    with open(SEED_BANK_PATH, "r", encoding="utf-8") as f:
        seed_data = json.load(f)
    seed = seed_data.get("quotes", [])

    known_texts = all_known_quote_texts()
    session_texts = [q["text"] for q in pool]
    needed = QUOTE_POOL_TARGET - len(pool)
    added = []

    for candidate in seed:
        if len(added) >= needed:
            break
        text = candidate.get("text", "")
        score = candidate.get("score", 0)
        if not text or score < MIN_ADMIT_SCORE:
            continue
        if text in known_texts:
            continue
        is_dup, _, _ = check_semantic_duplicate(text, list(known_texts) + session_texts)
        if is_dup:
            continue
        if check_pattern_repetition(text, session_texts, max_repeat=2, window=20):
            continue
        entry = {
            "id": str(uuid.uuid4()), "text": text, "author": candidate.get("author"),
            "category": candidate.get("category", "ORIGINAL"), "theme": candidate.get("theme"),
            "mood": candidate.get("mood"), "score": score,
            "hash": candidate.get("hash") or quote_hash(text),
            "pattern_tags": candidate.get("pattern_tags") or sorted(detect_sentence_patterns(text)),
        }
        pool.append(entry)
        added.append(entry)
        session_texts.append(text)
        known_texts.add(text)

    added_texts = {a["text"] for a in added}
    remaining_seed = [c for c in seed if c.get("text") not in added_texts]
    with open(SEED_BANK_PATH, "w", encoding="utf-8") as f:
        json.dump({**seed_data, "quotes": remaining_seed}, f, ensure_ascii=False, indent=2)
        f.write("\n")

    pool_data["quotes"] = pool
    pool_data["engine_version"] = ORIGINAL_QUOTE_ENGINE_VERSION
    with open(POOL_PATH, "w", encoding="utf-8") as f:
        json.dump(pool_data, f, ensure_ascii=False, indent=2)
        f.write("\n")

    report.update({"added": len(added), "pool_after": len(pool), "seed_bank_remaining": len(remaining_seed)})
    if len(pool) < QUOTE_POOL_TARGET:
        report["note"] = (f"Seed bank'ten {len(added)} söz eklendi ama hedefe ({QUOTE_POOL_TARGET}) ulaşılamadı "
                           f"(şu an {len(pool)}, seed bank'te {len(remaining_seed)} kaldı) -- yeni bir yazım turu gerekiyor.")
    else:
        report["note"] = f"Seed bank'ten {len(added)} söz eklendi, hedefe ({QUOTE_POOL_TARGET}) ulaşıldı."
    return report
