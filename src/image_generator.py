"""AI image generation module.

Priority order (per project policy -- never fall back to a random internet
image):
  1. find_local_media() -- a real, unused photo already sitting under
     media/library/<theme>/
  2. generate_image() -- AI-generated via the free Hugging Face Inference
     Providers text-to-image API (default) or, only if explicitly opted into,
     OpenAI's paid Images API
  3. neither available -> caller must set the queue item to "needs_generation"

Provider selection: IMAGE_PROVIDER env var, default "huggingface". The user
explicitly does not want to spend money on this, so "huggingface" is the only
provider used unless IMAGE_PROVIDER=openai is set by hand later -- there is no
automatic fallback from the free provider to the paid one for any reason
(quota exhaustion included). If the free provider fails for any reason
(missing HF_TOKEN, exhausted $0.10/month free credit, transient error), the
caller gets a RuntimeError and marks the slot needs_generation instead of ever
reaching for a paid API.

Hugging Face Inference Providers (verified 2026-08-31,
https://huggingface.co/docs/inference-providers/en/index and .../pricing):
free HF accounts get $0.10/month of credit usable on Inference Providers
(text-to-image included), no credit card required to sign up or to get that
credit. Once it's used up, requests fail (HTTP 402) rather than silently
charging a card -- there's no card on file at all for a free account. Model
used: black-forest-labs/FLUX.1-schnell, a fast/cheap open model chosen to
stretch that $0.10 as far as possible.

There is currently no automated vision-based QC step (that used a paid vision
model before OpenAI was dropped per the user's instruction) -- only the
structural checks in content_quality.check_image (corrupt file, resolution,
aspect ratio) run automatically. Anatomical defects ("bozuk el/yüz") are not
detected automatically in this free-only setup; a human glance before first
use of a new prompt template is still worth it.
"""
import json
import os
import time
from pathlib import Path

from src.content_bank import generate_image_prompt
from src.content_history import file_fingerprint, load_history
from src.content_quality import check_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = PROJECT_ROOT / "media" / "library"
GENERATED_DIR = PROJECT_ROOT / "media" / "generated"
GEN_LOG_PATH = PROJECT_ROOT / "logs" / "image_generation_log.jsonl"

IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "huggingface")  # "huggingface" (free, default) or "openai" (opt-in, paid)
HF_MODEL = os.environ.get("HF_IMAGE_MODEL", "black-forest-labs/FLUX.1-schnell")
OPENAI_IMAGE_MODEL = "gpt-image-2"
OPENAI_IMAGE_QUALITY = os.environ.get("IMAGE_GEN_QUALITY", "medium")  # low/medium/high

FEED_SIZE = (1088, 1088)   # >=1080px short side (content_quality.MIN_SHORT_SIDE_PX), multiple of 16 for the diffusion backend
REELS_SIZE = (1088, 1920)  # 9:16-ish, short side >=1080px, both dims multiples of 16

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp"}

import random  # noqa: E402  (kept near its only use below)


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


class QuotaExhaustedError(RuntimeError):
    """Raised when the free provider reports the free credit/quota is used
    up. Callers must treat this the same as any other generation failure
    (mark needs_generation) and must NEVER react to it by switching providers."""


def _hf_token() -> str:
    token = os.environ.get("HF_TOKEN", "").strip()
    if not token:
        raise RuntimeError("HF_TOKEN ayarlı değil")
    return token


def _call_huggingface_image_api(prompt: str, size: tuple[int, int]):
    """Returns a PIL.Image. Raises QuotaExhaustedError on HTTP 402 (free
    credit exhausted) and RuntimeError on any other failure."""
    from huggingface_hub import InferenceClient
    from huggingface_hub.errors import HfHubHTTPError

    client = InferenceClient(token=_hf_token())
    width, height = size
    try:
        try:
            return client.text_to_image(prompt, model=HF_MODEL, width=width, height=height)
        except TypeError:
            # some providers behind this model don't accept width/height kwargs
            return client.text_to_image(prompt, model=HF_MODEL)
    except HfHubHTTPError as e:
        status = getattr(e.response, "status_code", None)
        if status == 402:
            raise QuotaExhaustedError(
                "Hugging Face ücretsiz aylık kredisi (~$0.10) tükendi. "
                "Otomatik olarak ücretli bir servise geçilmiyor -- bu istek atlanacak."
            ) from e
        raise RuntimeError(f"Hugging Face Inference API hatası (HTTP {status}): {e}") from e


def _call_openai_image_api(prompt: str, size: tuple[int, int]):
    """Opt-in only (IMAGE_PROVIDER=openai). Not used by default. Returns a PIL.Image."""
    import base64
    import io
    import requests

    key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not key:
        raise RuntimeError("OPENAI_API_KEY ayarlı değil")
    size_str = f"{size[0]}x{size[1]}"
    resp = requests.post(
        "https://api.openai.com/v1/images/generations",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": OPENAI_IMAGE_MODEL, "prompt": prompt, "size": size_str,
            "quality": OPENAI_IMAGE_QUALITY, "n": 1, "output_format": "jpeg", "moderation": "auto",
        },
        timeout=120,
    )
    if resp.status_code >= 400:
        raise RuntimeError(f"OpenAI Images API hata verdi (HTTP {resp.status_code}): {resp.text[:300]}")
    from PIL import Image
    b64 = resp.json()["data"][0]["b64_json"]
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _generate_raw_image(prompt: str, size: tuple[int, int]):
    if IMAGE_PROVIDER == "openai":
        return _call_openai_image_api(prompt, size)
    return _call_huggingface_image_api(prompt, size)


def generate_image(theme: str, item_id: str, is_reels: bool = False, max_retries: int = 3,
                    prompt: str | None = None) -> Path:
    """Generates and saves an image to media/generated/{item_id}.jpg, retrying
    on transient failure or a structural defect (resolution/aspect ratio).
    Raises RuntimeError (or QuotaExhaustedError) if all attempts fail --
    callers must treat that as "no image available", never as a signal to
    try a different (paid) provider."""
    if IMAGE_PROVIDER == "openai":
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY ayarlı değil")
    else:
        _hf_token()  # fail fast (no retries) if the free provider's token just isn't configured

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{item_id}.jpg"
    size = REELS_SIZE if is_reels else FEED_SIZE
    prompt = prompt or generate_image_prompt(theme)

    last_error = "bilinmeyen hata"
    for attempt in range(1, max_retries + 1):
        try:
            image = _generate_raw_image(prompt, size)
            image.convert("RGB").save(out_path, format="JPEG", quality=92)

            structural_issues = check_image(str(out_path), is_reels=is_reels)
            if structural_issues:
                last_error = "; ".join(structural_issues)
                _log({"level": "warning", "item_id": item_id, "attempt": attempt, "message": last_error})
                continue

            _log({"level": "success", "item_id": item_id, "attempt": attempt, "path": str(out_path),
                  "provider": IMAGE_PROVIDER, "prompt": prompt})
            return out_path

        except QuotaExhaustedError as e:
            _log({"level": "error", "item_id": item_id, "attempt": attempt, "message": str(e)})
            out_path.unlink(missing_ok=True)
            raise  # no point retrying -- the quota won't refill mid-run

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
