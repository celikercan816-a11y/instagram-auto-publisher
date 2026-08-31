"""AI image generation module.

Priority order (per project policy -- never fall back to a random internet
image):
  1. src/image_generator.find_local_media() -- a real, unused photo already
     sitting under media/library/<theme>/
  2. generate_image() -- AI-generated via OpenAI's Images API
  3. neither available -> caller must set the queue item to "needs_generation"

Uses OpenAI's Images API (model "gpt-image-2", verified current as of
2026-08-31: https://developers.openai.com/api/docs/guides/image-generation).
Requires OPENAI_API_KEY. Never logs or prints the key.

Also runs a same-provider vision QC pass (chat completions, model
"gpt-5-mini") on every generated image to catch the "bozuk el/yüz/nesne"
(broken hands/face/object) case the user explicitly called out -- this is the
one thing a purely structural check (resolution/aspect ratio) can't catch.
"""
import base64
import json
import os
import random
import time
from pathlib import Path

import requests

from src.content_bank import generate_image_prompt
from src.content_history import file_fingerprint, load_history
from src.content_quality import check_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = PROJECT_ROOT / "media" / "library"
GENERATED_DIR = PROJECT_ROOT / "media" / "generated"
GEN_LOG_PATH = PROJECT_ROOT / "logs" / "image_generation_log.jsonl"

IMAGE_MODEL = "gpt-image-2"
VISION_QC_MODEL = "gpt-5-mini"
IMAGE_QUALITY = os.environ.get("IMAGE_GEN_QUALITY", "medium")  # low/medium/high
FEED_SIZE = "1024x1024"
REELS_SIZE = "1080x1920"

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}


def _log(event: dict) -> None:
    GEN_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    from datetime import datetime, timezone
    event = {"timestamp": datetime.now(timezone.utc).isoformat(), **event}
    with open(GEN_LOG_PATH, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, ensure_ascii=False) + "\n")


def find_local_media(theme: str) -> Path | None:
    """Returns an unused image from media/library/<theme>/, or None."""
    theme_dir = LIBRARY_DIR / theme
    if not theme_dir.exists():
        return None
    used_fingerprints = {e.get("image_fingerprint") for e in load_history()}
    candidates = [p for p in theme_dir.iterdir() if p.suffix.lower() in IMAGE_EXTS]
    random.shuffle(candidates)
    for path in candidates:
        if file_fingerprint(path) not in used_fingerprints:
            return path
    return None


def _openai_api_key() -> str:
    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY ayarlı değil")
    return key


def _call_openai_image_api(prompt: str, size: str) -> bytes:
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {_openai_api_key()}"},
        json={
            "model": IMAGE_MODEL,
            "prompt": prompt,
            "size": size,
            "quality": IMAGE_QUALITY,
            "n": 1,
            "output_format": "jpeg",
            "moderation": "auto",
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI Images API hata verdi (HTTP {resp.status_code}): {resp.text[:300]}")
    data = resp.json()
    b64 = data["data"][0]["b64_json"]
    return base64.b64decode(b64)


def _vision_qc(image_path: Path, theme: str) -> tuple[bool, str]:
    """Asks a vision-capable model to flag broken anatomy/objects or an
    identifiable real-looking face. Fails open (returns ok=True with a
    warning reason) if the QC call itself errors, so a transient API/schema
    issue never becomes a hard block."""
    try:
        b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
        resp = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {_openai_api_key()}"},
            json={
                "model": VISION_QC_MODEL,
                "messages": [{
                    "role": "user",
                    "content": [
                        {"type": "text", "text": (
                            "You are a strict QC reviewer for an Instagram content pipeline. "
                            f"This is an AI-generated '{theme}' photo. Reply with ONLY one word: "
                            "REJECT if it shows a distorted/broken hand, face, or object, or if it "
                            "shows a clearly identifiable, photorealistic human face (which risks "
                            "impersonating a real person); otherwise reply OK."
                        )},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                    ],
                }],
                "max_tokens": 5,
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            return True, f"vision QC atlandı (HTTP {resp.status_code})"
        verdict = resp.json()["choices"][0]["message"]["content"].strip().upper()
        if "REJECT" in verdict:
            return False, "Vision QC: bozuk anatomi/nesne veya tanınabilir gerçek yüz riski tespit etti"
        return True, "vision QC: OK"
    except Exception as e:
        return True, f"vision QC atlandı (hata: {e})"


def generate_image(theme: str, item_id: str, is_reels: bool = False, max_retries: int = 3,
                    prompt: str | None = None) -> Path:
    """Generates and saves an image to media/generated/{item_id}.jpg, retrying
    on API failure, structural defects (resolution/aspect) or a vision-QC
    rejection. Raises RuntimeError if all attempts fail."""
    _openai_api_key()  # fail fast (no retries) if the key just isn't configured

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{item_id}.jpg"
    size = REELS_SIZE if is_reels else FEED_SIZE
    prompt = prompt or generate_image_prompt(theme)

    last_error = "bilinmeyen hata"
    for attempt in range(1, max_retries + 1):
        try:
            image_bytes = _call_openai_image_api(prompt, size)
            out_path.write_bytes(image_bytes)

            structural_issues = check_image(str(out_path), is_reels=is_reels)
            if structural_issues:
                last_error = "; ".join(structural_issues)
                _log({"level": "warning", "item_id": item_id, "attempt": attempt, "message": last_error})
                continue

            ok, reason = _vision_qc(out_path, theme)
            if not ok:
                last_error = reason
                _log({"level": "warning", "item_id": item_id, "attempt": attempt, "message": reason})
                continue

            _log({"level": "success", "item_id": item_id, "attempt": attempt, "path": str(out_path),
                  "prompt": prompt, "vision_qc": reason})
            return out_path

        except Exception as e:
            last_error = str(e)
            _log({"level": "error", "item_id": item_id, "attempt": attempt, "message": last_error})
            time.sleep(2)

    out_path.unlink(missing_ok=True)
    raise RuntimeError(f"Görsel üretimi {max_retries} denemede başarısız oldu: {last_error}")


def get_media_for_theme(theme: str, item_id: str, is_reels: bool = False) -> tuple[Path, str, str]:
    """Returns (path, media_source, image_prompt). image_prompt is '' for
    local media. Tries local library first, then AI generation."""
    local = find_local_media(theme)
    if local:
        return local, "local", ""
    prompt = generate_image_prompt(theme)
    generated = generate_image(theme, item_id, is_reels=is_reels, prompt=prompt)
    return generated, "ai_generated", prompt
