"""AI image generation module.

Priority order (per project policy -- never fall back to a random internet
image):
  1. find_local_media() -- a real, unused photo already sitting under
     media/library/<theme>/
  2. generate_image() -- AI-generated via the free Hugging Face Inference
     Providers text-to-image API (default) or, only if explicitly opted into,
     OpenAI's paid Images API
  3. neither available -> caller must set the queue item to "needs_generation"

Provider selection: IMAGE_PROVIDER env var, default "huggingface" (unchanged
-- the existing automated pipeline's behavior is not altered by adding
Cloudflare support until this default is deliberately changed). Recognized
values:
  "huggingface" (default) -- only Hugging Face, exactly as before.
  "cloudflare"             -- only Cloudflare Workers AI.
  "auto"                   -- Cloudflare first (10,000 free Neurons/day, much
                               more headroom than HF's $0.10/month), falling
                               back to Hugging Face if Cloudflare errors,
                               rate-limits, or hits its daily free quota.
  "openai"                 -- opt-in, paid, unchanged from before.
No mode EVER falls back to a paid provider automatically -- if every free
option in the chosen mode fails, the caller gets a RuntimeError/
QuotaExhaustedError-family exception and must mark the slot
needs_generation, exactly as before.

Hugging Face Inference Providers (verified 2026-08-31,
https://huggingface.co/docs/inference-providers/en/index and .../pricing):
free HF accounts get $0.10/month of credit usable on Inference Providers
(text-to-image included), no credit card required to sign up or to get that
credit. Once it's used up, requests fail (HTTP 402) rather than silently
charging a card -- there's no card on file at all for a free account. Model
used: black-forest-labs/FLUX.1-schnell, a fast/cheap open model chosen to
stretch that $0.10 as far as possible.

Cloudflare Workers AI (verified 2026-09-01 against developers.cloudflare.com):
10,000 free Neurons/day, no card required for the free "Workers Free" plan.
flux-1-schnell costs 4.80 Neurons/512x512 tile + 9.60 Neurons/step (4 steps
default) = ~43.2 Neurons/image, i.e. roughly 230 free images/day -- far more
headroom than HF. Exceeding the daily allocation fails with HTTP/error code
4006 (request rejected, confirmed NOT auto-billed) unless the account is
explicitly upgraded to Workers Paid, which this project never does. The
model has no width/height parameter -- it always generates at a fixed
~512x512 tile; generate_image() resizes/crops locally afterward same as any
other provider whose native resolution differs from the target size.

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

from PIL import Image

from src.content_bank import generate_image_prompt, resolve_theme
from src.content_history import file_fingerprint, load_history
from src.content_quality import check_image

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LIBRARY_DIR = PROJECT_ROOT / "media" / "library"
GENERATED_DIR = PROJECT_ROOT / "media" / "generated"
GEN_LOG_PATH = PROJECT_ROOT / "logs" / "image_generation_log.jsonl"

IMAGE_PROVIDER = os.environ.get("IMAGE_PROVIDER", "huggingface")  # "huggingface" (free, default) / "cloudflare" (free) / "auto" (cloudflare->huggingface) / "openai" (opt-in, paid)
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
    theme_dir = LIBRARY_DIR / resolve_theme(theme)
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


CLOUDFLARE_MODEL = os.environ.get("CLOUDFLARE_IMAGE_MODEL", "@cf/black-forest-labs/flux-1-schnell")
CLOUDFLARE_NATIVE_SIZE = (512, 512)  # fixed -- this model takes no width/height param


class CloudflareQuotaExhaustedError(RuntimeError):
    """Raised when Cloudflare Workers AI reports the daily free-Neuron
    allocation (10,000/day, Workers Free plan) is exhausted, or the request
    is otherwise rate-limited. In "auto" mode this triggers a fallback to
    Hugging Face; in standalone "cloudflare" mode it must be treated like
    QuotaExhaustedError -- never react to it by paying."""


class CloudflareConfigError(RuntimeError):
    """CLOUDFLARE_ACCOUNT_ID or CLOUDFLARE_API_TOKEN missing/blank."""


def _cloudflare_credentials() -> tuple[str, str]:
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        raise CloudflareConfigError("CLOUDFLARE_ACCOUNT_ID / CLOUDFLARE_API_TOKEN ayarlı değil")
    return account_id, token


def _call_cloudflare_image_api(prompt: str, size: tuple[int, int]):
    """Returns a PIL.Image from Cloudflare Workers AI (@cf/black-forest-labs/
    flux-1-schnell). This model has no width/height parameter -- it always
    generates at a fixed native tile (CLOUDFLARE_NATIVE_SIZE); generate_image()
    resizes/crops locally afterward, same as it would for any provider whose
    native output doesn't match the target size. Raises
    CloudflareQuotaExhaustedError on daily free-quota exhaustion/rate-limit
    (HTTP 429 or Cloudflare error code 4006), CloudflareConfigError if
    credentials aren't set, RuntimeError on any other failure. Never falls
    back to a paid Cloudflare plan -- this account stays on Workers Free."""
    import base64
    import io

    import requests

    account_id, token = _cloudflare_credentials()
    prompt = prompt[:2048]  # documented request limit
    url = f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/run/{CLOUDFLARE_MODEL}"
    try:
        resp = requests.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"prompt": prompt, "steps": 4},
            timeout=60,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"Cloudflare Workers AI'a bağlanılamadı: {e}") from e

    if resp.status_code == 429 or "4006" in resp.text:
        raise CloudflareQuotaExhaustedError(
            "Cloudflare Workers AI günlük ücretsiz Neuron kotası (10.000/gün) tükendi "
            "veya istek geçici olarak sınırlandı. Otomatik olarak ücretli plana geçilmiyor."
        )
    if resp.status_code >= 400:
        raise RuntimeError(f"Cloudflare Workers AI hatası (HTTP {resp.status_code}): {resp.text[:300]}")

    from PIL import Image

    content_type = resp.headers.get("content-type", "")
    if content_type.startswith("image/"):
        return Image.open(io.BytesIO(resp.content))

    data = resp.json()
    if data.get("success") is False:
        errors = data.get("errors") or []
        codes = [e.get("code") for e in errors]
        if 4006 in codes:
            raise CloudflareQuotaExhaustedError("Cloudflare Workers AI günlük ücretsiz Neuron kotası tükendi.")
        raise RuntimeError(f"Cloudflare Workers AI hata döndürdü: {errors}")
    result = data.get("result", data)
    b64 = result.get("image") if isinstance(result, dict) else None
    if not b64:
        raise RuntimeError(f"Cloudflare Workers AI beklenmeyen yanıt formatı: {list(data.keys())}")
    return Image.open(io.BytesIO(base64.b64decode(b64)))


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


def _generate_raw_image(prompt: str, size: tuple[int, int]) -> tuple:
    """Returns (PIL.Image, provider_used, model_used)."""
    if IMAGE_PROVIDER == "openai":
        return _call_openai_image_api(prompt, size), "openai", OPENAI_IMAGE_MODEL
    if IMAGE_PROVIDER == "cloudflare":
        return _call_cloudflare_image_api(prompt, size), "cloudflare", CLOUDFLARE_MODEL
    if IMAGE_PROVIDER == "auto":
        try:
            return _call_cloudflare_image_api(prompt, size), "cloudflare", CLOUDFLARE_MODEL
        except (CloudflareQuotaExhaustedError, CloudflareConfigError, RuntimeError) as e:
            _log({"level": "warning", "message": f"Cloudflare kullanılamadı, Hugging Face'e düşülüyor: {e}"})
            return _call_huggingface_image_api(prompt, size), "huggingface", HF_MODEL
    return _call_huggingface_image_api(prompt, size), "huggingface", HF_MODEL


def generate_image(theme: str, item_id: str, is_reels: bool = False, max_retries: int = 3,
                    prompt: str | None = None, meta_out: dict | None = None) -> Path:
    """Generates and saves an image to media/generated/{item_id}.jpg, retrying
    on transient failure or a structural defect (resolution/aspect ratio).
    Raises RuntimeError (or QuotaExhaustedError/CloudflareQuotaExhaustedError)
    if all attempts fail -- callers must treat that as "no image available",
    never as a signal to try a different (paid) provider.

    meta_out, if given a dict, is filled in-place with provider/model/
    resolution/duration info about the successful attempt -- purely additive,
    existing callers that don't pass it see no behavior change."""
    if IMAGE_PROVIDER == "openai":
        if not os.environ.get("OPENAI_API_KEY", "").strip():
            raise RuntimeError("OPENAI_API_KEY ayarlı değil")
    elif IMAGE_PROVIDER == "cloudflare":
        _cloudflare_credentials()  # fail fast if not configured
    elif IMAGE_PROVIDER == "auto":
        pass  # each branch inside _generate_raw_image checks its own credentials
    else:
        _hf_token()  # fail fast (no retries) if the free provider's token just isn't configured

    GENERATED_DIR.mkdir(parents=True, exist_ok=True)
    out_path = GENERATED_DIR / f"{item_id}.jpg"
    size = REELS_SIZE if is_reels else FEED_SIZE
    prompt = prompt or generate_image_prompt(theme)

    last_error = "bilinmeyen hata"
    for attempt in range(1, max_retries + 1):
        try:
            t0 = time.monotonic()
            image, provider_used, model_used = _generate_raw_image(prompt, size)
            native_size = image.size
            elapsed = time.monotonic() - t0
            final_image = image.convert("RGB")
            if final_image.size != size:
                final_image = final_image.resize(size, Image.LANCZOS)
            final_image.save(out_path, format="JPEG", quality=92)

            structural_issues = check_image(str(out_path), is_reels=is_reels)
            if structural_issues:
                last_error = "; ".join(structural_issues)
                _log({"level": "warning", "item_id": item_id, "attempt": attempt, "message": last_error})
                continue

            _log({"level": "success", "item_id": item_id, "attempt": attempt, "path": str(out_path),
                  "provider": provider_used, "model": model_used, "native_size": native_size,
                  "duration_s": round(elapsed, 2), "prompt": prompt})
            if meta_out is not None:
                meta_out.update({
                    "image_provider": provider_used, "image_model": model_used,
                    "generation_status": "ok", "generation_error": None,
                    "native_resolution": native_size, "final_resolution": size,
                    "generation_time_s": round(elapsed, 2),
                })
            return out_path

        except (QuotaExhaustedError, CloudflareQuotaExhaustedError) as e:
            _log({"level": "error", "item_id": item_id, "attempt": attempt, "message": str(e)})
            out_path.unlink(missing_ok=True)
            if meta_out is not None:
                meta_out.update({"generation_status": "needs_generation", "generation_error": str(e)})
            raise  # no point retrying -- the quota won't refill mid-run

        except Exception as e:
            last_error = str(e)
            _log({"level": "error", "item_id": item_id, "attempt": attempt, "message": last_error})
            time.sleep(2)

    out_path.unlink(missing_ok=True)
    if meta_out is not None:
        meta_out.update({"generation_status": "needs_generation", "generation_error": last_error})
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
