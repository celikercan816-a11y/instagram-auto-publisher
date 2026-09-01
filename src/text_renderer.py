"""Local Pillow-based text rendering for the "manzara + söz" content pivot
(2026-09-01) -- point 9/10/11/12 of the approved design, revised 2026-09-01
after the first 3-post test surfaced real problems (text landing on a lit
bridge in test #2; a stray "/" in the grid's metadata display; inflated
quality scores that didn't reflect placement quality). The AI image model
NEVER renders the quote text itself (point 3's explicit rule); this module
draws it on top of an already-generated clean background, entirely locally,
using a bundled font so Turkish characters (ğ ü ş ı ö ç İ Ğ Ü Ş Ö Ç) always
render correctly regardless of what fonts happen to be installed on the
machine (verified against assets/fonts/PlayfairDisplay-*.ttf).

Four templates, a fixed 1080x1350 (4:5) canvas with a 90px minimum safe
margin, and a hard rule: text needing more than 4 lines at a legible size is
REJECTED (fit_ok=False), never force-shrunk into illegibility.

INTELLIGENT PLACEMENT (new): each template has a natural vertical zone
(A=top, B=center, C=bottom, D=upper-middle) that gives it its visual
identity, but the exact position is no longer fixed -- _choose_zone() scores
that zone plus its two alternatives for brightness (90th-percentile, so a
bright patch anywhere in the band counts) and edge density (Sobel-style
gradient magnitude -- a proxy for "visual clutter"), and only overrides the
template's natural zone if it is meaningfully worse than another. This is
what stops text from landing on something like a brightly-lit bridge (the
exact failure seen in the first test batch): the busy/bright zone scores
worse and rendering falls back to a cleaner one instead.

UNIFIED BRAND TEMPLATE (2026-09-01, replaces A/B/C/D as the default): after
reviewing three side-by-side layout alternatives, the account settled on a
single design language -- Template A's ("editorial") visual identity: an
elegant normal-weight serif, white/off-white text, a small restrained quote
mark and a short thin underline used only some of the time, minimal
uppercase, minimal italics (author line only). render_quote_editorial()
implements this. Placement itself is explicitly NOT fixed to one spot: it
scores all 9 rule-of-thirds grid cells (top/center/bottom x left/center/
right) for brightness+edge-density cleanliness, adds a fixed centrality
penalty so a merely-clean center cell doesn't automatically win (there is no
semantic segmentation available to detect the actual subject/horizon --
documented limitation, same policy as the rest of this module), and refuses
to repeat the same cell 3 times in a row given the caller's recent-zone
history. Typography varies quote-to-quote (2 vs 3 lines from the natural
wrap, quote-mark and underline shown or not) via a deterministic hash of the
quote text, so a given quote always renders identically on re-generation but
different quotes vary -- while every render still reads as the same premium
family. The old per-template A/B/C/D functions are kept (not deleted) but
are no longer the default path.
"""
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FONT_DIR = PROJECT_ROOT / "assets" / "fonts"

CANVAS_SIZE = (1080, 1350)  # 4:5 Instagram feed portrait
SAFE_MARGIN = 90
# Bottom-anchored text technically clearing SAFE_MARGIN still read as jammed
# into the very edge of the frame -- a purely technical pass isn't the same
# as looking comfortable. Applies only to the "bottom" row of the 9-zone
# grid; center rows keep the normal SAFE_MARGIN.
BOTTOM_VISUAL_MARGIN = 150
# Same idea for the "top" row -- a post whose text starts right at the
# technical safe margin can look cramped/clipped-feeling near the top edge.
TOP_VISUAL_MARGIN = 150
MAX_LINES = 4
MIN_FONT_SIZE = 34
TEMPLATES = ["A", "B", "C", "D"]

# Palette: white/off-white primary, light grey secondary, warm cream/gold-
# beige as a rare accent -- never neon.
COLOR_PRIMARY = (250, 248, 244)
COLOR_SECONDARY = (214, 210, 201)
COLOR_ACCENT = (222, 198, 150)

# Bundled fonts first (guarantees identical rendering on Windows and inside
# GitHub Actions' ubuntu runner); common system fonts as a fallback so the
# page still renders something reasonable if the bundled files are ever
# missing, PIL's built-in bitmap font as the last resort (logged as a
# warning -- it does not support Turkish characters well and should be
# treated as a "something is misconfigured" signal, not normal operation).
_SERIF_CANDIDATES = [
    FONT_DIR / "PlayfairDisplay-Regular.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
]
_SERIF_ITALIC_CANDIDATES = [
    FONT_DIR / "PlayfairDisplay-Italic.ttf",
    "C:/Windows/Fonts/georgiai.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
]


def _find_font(candidates: list) -> Path | str | None:
    for c in candidates:
        if Path(c).exists():
            return c
    return None


def check_font_availability() -> dict:
    """Local, synchronous check -- 'font bulunabilirliğini local olarak
    kontrol et'. Returns which serif/italic font path will actually be
    used, and whether it's the bundled one (best) or a fallback."""
    serif = _find_font(_SERIF_CANDIDATES)
    italic = _find_font(_SERIF_ITALIC_CANDIDATES)
    return {
        "serif_path": str(serif) if serif else None,
        "italic_path": str(italic) if italic else None,
        "using_bundled": serif == _SERIF_CANDIDATES[0] if serif else False,
        "ok": serif is not None,
    }


def _load_font(path, size: int) -> ImageFont.FreeTypeFont:
    if path is None:
        return ImageFont.load_default()
    return ImageFont.truetype(str(path), size)


CONTRAST_LUMINANCE_MAX = 130  # background behind white/cream text (~245) must be darker than this


@dataclass
class RenderResult:
    image: Image.Image
    template: str
    font_size: int
    lines: list[str]
    fit_ok: bool
    rejection_reason: str | None
    bg_luminance_at_text: float = 0.0
    zone: str = ""
    zone_fallback_used: bool = False
    placement_score: float = 0.0  # 0 (worst) .. 1 (best) -- how clean the chosen zone was
    line_length_balance: float = 1.0  # 0 (very uneven) .. 1 (very even) line widths
    # Absolute pixel bounding box of the ENTIRE drawn text block (quote mark
    # + lines + underline + author, whichever are present) on the final
    # CANVAS_SIZE image -- None for the legacy per-template render_quote()
    # path, which doesn't track this. Used by quote_quality's grid-crop
    # safety check (point 5: does Instagram's square profile-grid crop clip
    # any of this?).
    text_top: int | None = None
    text_bottom: int | None = None
    text_left: int | None = None
    text_right: int | None = None

    @property
    def contrast_ok(self) -> bool:
        return self.bg_luminance_at_text <= CONTRAST_LUMINANCE_MAX


def _wrap_to_width(measure_fn, text: str, max_width: int) -> list[str]:
    """Respects explicit '\\n' (poem line breaks) and additionally word-wraps
    any line that's still too wide for max_width. measure_fn(line) -> pixel
    width must reflect EXACTLY what will be drawn (case transform, manual
    letter-tracking included) -- using a plain/untransformed measurement here
    was the root cause of a real text-overflow bug (Template D uppercases and
    letter-spaces the text at draw time; wrapping against the untransformed
    width let lines through that were, once transformed, wider than the
    frame -- confirmed both left- and right-edge overflow)."""
    lines = []
    for raw_line in text.split("\n"):
        if not raw_line.strip():
            continue
        words = raw_line.split()
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if measure_fn(candidate) <= max_width or not current:
                current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def _line_widths(measure_fn, lines: list[str]) -> list[int]:
    return [measure_fn(ln) for ln in lines]


def _comma_split(text: str) -> list[str]:
    """Semantic line-break candidate: split after clause-ending punctuation
    (comma/semicolon/colon) so 'Şehir unutmaz, sadece biriktirir.' prefers
    breaking into its two natural clauses instead of an arbitrary
    width-driven word boundary."""
    import re
    parts = re.split(r"(?<=[,;:])\s+", text.strip())
    return [p for p in parts if p]


def _tracked_text_width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, tracking: int) -> int:
    """Exact width of text as _draw_tracked_text will actually render it
    (per-character advance + manual letter-spacing) -- must match that
    function's math so fit-time measurement equals draw-time width."""
    if not text:
        return 0
    return sum(draw.textbbox((0, 0), ch, font=font)[2] + tracking for ch in text) - tracking


def _balance_score(widths: list[int]) -> float:
    """1.0 = all lines the same width, 0.0 = wildly uneven (a single-word
    orphan line, etc.) -- point 5's 'satır uzunlukları birbirine yakın'."""
    if len(widths) <= 1:
        return 1.0
    mean = sum(widths) / len(widths)
    if mean == 0:
        return 1.0
    variance = sum((w - mean) ** 2 for w in widths) / len(widths)
    cv = (variance ** 0.5) / mean  # coefficient of variation
    return max(0.0, 1.0 - min(cv, 1.0))


def _best_balanced_wrap(measure_fn, text: str, max_width: int, max_lines: int = MAX_LINES) -> tuple[list[str], float]:
    """Tries a few narrower target widths (which push more words onto the
    next line) plus a semantic comma-based split, and keeps whichever still
    fits max_lines, stays within max_width under the REAL measurement, and
    yields the most EVEN/natural line lengths -- instead of always greedily
    filling to max_width (which could leave a short orphan last line, or
    prefer an arbitrary word break over a natural clause break)."""
    candidate_sets = [_wrap_to_width(measure_fn, text, int(max_width * f)) for f in (1.0, 0.88, 0.78, 0.68)]
    comma_lines = _comma_split(text)
    is_comma_candidate = len(comma_lines) > 1
    if is_comma_candidate:
        candidate_sets.append(comma_lines)

    best_lines, best_score = None, -1.0
    for candidate_lines in candidate_sets:
        if len(candidate_lines) == 0 or len(candidate_lines) > max_lines:
            continue
        widths = _line_widths(measure_fn, candidate_lines)
        if max(widths, default=0) > max_width:
            continue  # narrower target still overflowed a word -- invalid
        score = _balance_score(widths)
        if is_comma_candidate and candidate_lines is comma_lines:
            score += 0.02  # small tie-break bonus for a natural clause break
        if score > best_score:
            best_lines, best_score = candidate_lines, score
    if best_lines is None:
        best_lines = _wrap_to_width(measure_fn, text, max_width)
        best_score = _balance_score(_line_widths(measure_fn, best_lines))
    return best_lines, best_score


def _fit_text(text: str, max_width: int, max_font_size: int, font_path,
              transform=None, tracked: bool = False, max_lines: int = MAX_LINES) -> tuple[int, list[str], bool, str | None, float]:
    """Finds the largest font size (<=max_font_size, >=MIN_FONT_SIZE) that
    wraps the text into at most MAX_LINES reasonably-balanced lines within
    max_width. Returns (font_size, lines, fit_ok, rejection_reason,
    balance_score) -- fit_ok=False means the text must be shortened/
    rewritten, never force-shrunk further, and ALSO means "reject as
    overflow" (point 1: text overflow = hard fail) since it means no size
    down to MIN_FONT_SIZE could fit within the safe margins.

    transform/tracked let the caller (render_quote) pass the EXACT
    per-template drawing transform (Template D's uppercasing + manual
    letter-spacing) so the fitted width always matches the rendered width --
    measuring one thing and drawing another was the actual overflow bug."""
    probe_img = Image.new("RGB", (10, 10))
    draw = ImageDraw.Draw(probe_img)
    transform = transform or (lambda s: s)

    def _measure_fn(font, tracking):
        def measure(line: str) -> int:
            t = transform(line)
            if tracking:
                return _tracked_text_width(draw, t, font, tracking)
            return draw.textbbox((0, 0), t, font=font)[2]
        return measure

    for size in range(max_font_size, MIN_FONT_SIZE - 1, -2):
        font = _load_font(font_path, size)
        tracking = max(2, size // 22) if tracked else 0
        measure = _measure_fn(font, tracking)
        lines, balance = _best_balanced_wrap(measure, text, max_width, max_lines)
        # Defense-in-depth: _best_balanced_wrap already rejects any candidate
        # whose measured width exceeds max_width, so this should be
        # unreachable -- but a real overflow must NEVER slip through, so
        # re-verify explicitly rather than trust that invariant blindly.
        if len(lines) <= max_lines and max(_line_widths(measure, lines), default=0) <= max_width:
            return size, lines, True, None, balance

    font = _load_font(font_path, MIN_FONT_SIZE)
    tracking = max(2, MIN_FONT_SIZE // 22) if tracked else 0
    measure = _measure_fn(font, tracking)
    lines, balance = _best_balanced_wrap(measure, text, max_width, max_lines)
    return MIN_FONT_SIZE, lines, False, (
        f"Metin {max_lines} satıra, {MIN_FONT_SIZE}px minimum boyutta bile güvenli kenar boşluğu "
        f"içinde sığmıyor ({len(lines)} satır gerekiyor) -- metin kısaltılmalı."
    ), balance


def _percentile_brightness(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """90th percentile, not mean: a plain average over a tall text box gets
    diluted by dark sky/water above or below a bright band (e.g. a lit
    bridge cutting straight through the middle of the text) -- found via an
    actual test render where the mean under-reported the real readability
    problem. The 90th percentile reflects "is there a bright patch
    somewhere in here", which is what actually hurts legibility."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)
    if x1 <= x0 or y1 <= y0:
        return 128.0
    region = image.crop((x0, y0, x1, y1)).convert("L")
    return float(np.percentile(np.asarray(region), 90))


def _edge_density(image: Image.Image, box: tuple[int, int, int, int]) -> float:
    """Mean gradient magnitude -- a proxy for visual clutter/complexity
    (a plain sky scores low, a busy skyline or crowd scores high)."""
    x0, y0, x1, y1 = [int(v) for v in box]
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(image.width, x1), min(image.height, y1)
    if x1 - x0 < 2 or y1 - y0 < 2:
        return 0.0
    region = np.asarray(image.crop((x0, y0, x1, y1)).convert("L")).astype(np.float64)
    gy, gx = np.gradient(region)
    return float(np.sqrt(gx ** 2 + gy ** 2).mean())


def _zone_score(image: Image.Image, box: tuple[int, int, int, int]) -> tuple[float, float, float]:
    """Returns (combined_score, brightness, edge_density) for one candidate
    text box -- LOWER combined_score = cleaner/more readable-friendly zone.
    Brightness and edge density are both normalized to roughly 0..1 first."""
    brightness = _percentile_brightness(image, box)
    edges = _edge_density(image, box)
    norm_brightness = brightness / 255.0
    norm_edges = min(edges / 35.0, 1.0)
    return norm_brightness * 0.55 + norm_edges * 0.45, brightness, edges


# Each template's natural (identity-defining) zone, plus its fallback
# alternatives in preference order -- a fallback is only used if the natural
# zone scores meaningfully worse (see _choose_zone's MARGIN).
_TEMPLATE_ZONES = {
    "A": ["top", "upper_middle", "center", "bottom"],
    "B": ["center", "upper_middle", "top", "bottom"],
    "C": ["bottom", "center", "upper_middle", "top"],
    "D": ["upper_middle", "top", "center", "bottom"],
}
_ZONE_FALLBACK_MARGIN = 0.12  # natural zone must be within this of the best score to keep it


def _zone_box(zone: str, w: int, h: int, block_height: int, extra_bottom: int) -> tuple[int, tuple[int, int, int, int]]:
    """Returns (top_y, box) for a named zone, sized to fit block_height."""
    total_h = block_height + extra_bottom
    if zone == "top":
        top = SAFE_MARGIN + 30
    elif zone == "upper_middle":
        top = int(h * 0.30)
    elif zone == "center":
        top = (h - total_h) // 2
    else:  # bottom
        top = h - SAFE_MARGIN - total_h
    top = max(SAFE_MARGIN, min(top, h - SAFE_MARGIN - total_h))
    box = (0, max(0, top - 60), w, min(h, top + total_h + 60))
    return top, box


def _choose_zone(image: Image.Image, template: str, w: int, h: int, block_height: int, extra_bottom: int) -> tuple[int, str, bool, float]:
    """Intelligent placement: scores the template's natural zone against its
    listed alternatives and only switches away from it if meaningfully
    cleaner elsewhere. Returns (top_y, zone_name, fallback_used, placement_score
    [0..1, higher=cleaner])."""
    candidates = _TEMPLATE_ZONES[template]
    scored = []
    for zone in candidates:
        top, box = _zone_box(zone, w, h, block_height, extra_bottom)
        score, brightness, edges = _zone_score(image, box)
        scored.append((zone, top, score))

    natural_zone, natural_top, natural_score = scored[0]
    best_zone, best_top, best_score = min(scored, key=lambda t: t[2])

    if best_zone != natural_zone and (natural_score - best_score) > _ZONE_FALLBACK_MARGIN:
        chosen_zone, chosen_top, chosen_score, fallback = best_zone, best_top, best_score, True
    else:
        chosen_zone, chosen_top, chosen_score, fallback = natural_zone, natural_top, natural_score, False

    placement_score = max(0.0, 1.0 - chosen_score)
    return chosen_top, chosen_zone, fallback, placement_score


def _apply_overlay(image: Image.Image, box: tuple[int, int, int, int], base_opacity: float, target_luminance: float = 100) -> Image.Image:
    """A soft dark gradient (never a hard box) behind the text region only,
    strongest at the text's own vertical band and fading out over a wide,
    blurred falloff so it never reads as a flat rectangle.

    Opacity is ADAPTIVE: base_opacity is a floor for a normal/dark
    background; if the actual background there is brighter than
    target_luminance (checked via the same 90th-percentile measure used for
    zone scoring), opacity increases (capped at 0.6) until white/cream text
    would have real contrast against it -- without ever becoming a flat
    black box."""
    pre_brightness = _percentile_brightness(image, box)
    if pre_brightness > target_luminance:
        extra = (pre_brightness - target_luminance) / 255.0
        opacity = min(0.6, base_opacity + extra)
    else:
        opacity = base_opacity
    overlay = Image.new("L", image.size, 0)
    fade = 150
    grad = Image.new("L", (1, box[3] - box[1] + 2 * fade))
    for y in range(grad.height):
        edge_fade = min(y, grad.height - y, fade) / fade
        grad.putpixel((0, y), int(255 * opacity * min(1.0, edge_fade)))
    grad = grad.resize((image.width, grad.height))
    overlay.paste(grad, (0, max(0, box[1] - fade)))
    overlay = overlay.filter(ImageFilter.GaussianBlur(70))
    return _blend_dark(image, overlay)


def _blend_dark(image: Image.Image, alpha_mask: Image.Image) -> Image.Image:
    arr = np.asarray(image).astype(np.float64)
    alpha = np.asarray(alpha_mask).astype(np.float64) / 255.0
    darkened = arr * (1 - alpha[..., None])
    return Image.fromarray(np.clip(darkened, 0, 255).astype(np.uint8))


def _turkish_upper(text: str) -> str:
    """Python's str.upper() is not Turkish-aware: 'değil'.upper() ->
    'DEĞIL' (dotless I), which is linguistically wrong -- Turkish 'i'
    uppercases to dotted 'İ', and 'ı' uppercases to plain 'I'. Only
    TEMPLATE D uses uppercase text, so this is applied there specifically."""
    return text.replace("i", "İ").replace("ı", "I").upper()


def _draw_tracked_text(draw: ImageDraw.ImageDraw, xy: tuple[int, int], text: str, font: ImageFont.FreeTypeFont, fill, tracking: int, anchor_center_width: int | None = None) -> None:
    """Manual letter-spacing (PIL has no native tracking support) -- used
    only by TEMPLATE D's wide-spaced premium look."""
    x, y = xy
    if anchor_center_width is not None:
        total_w = sum(draw.textbbox((0, 0), ch, font=font)[2] + tracking for ch in text) - tracking
        x = xy[0] + (anchor_center_width - total_w) // 2
    for ch in text:
        draw.text((x, y), ch, font=font, fill=fill)
        w = draw.textbbox((0, 0), ch, font=font)[2]
        x += w + tracking


def render_quote(
    background: Image.Image,
    text: str,
    author: str | None = None,
    template: str | None = None,
    handle: str | None = None,
) -> RenderResult:
    """Renders `text` (and optional `author` attribution, optional small
    `handle` signature, default off/None) onto `background`, cropped/
    resized to CANVAS_SIZE. template is one of TEMPLATES, random if not
    given. Placement within the template's natural zone is chosen
    automatically by _choose_zone() -- see module docstring."""
    import random as _random

    template = template or _random.choice(TEMPLATES)
    fonts = check_font_availability()

    img = background.convert("RGB")
    img = _smart_crop_to_ratio(img, CANVAS_SIZE)
    w, h = img.size
    max_text_width = w - 2 * SAFE_MARGIN

    max_font_size = {"A": 72, "B": 64, "C": 60, "D": 56}[template]
    # Template D uppercases + manually letter-spaces its text (see
    # _draw_tracked_text below) and draws it in the ITALIC font file -- the
    # fit computation must use that exact same transform/tracking/font, or
    # the wrapped width silently diverges from the rendered width (the
    # actual cause of a confirmed left+right safe-margin overflow).
    fit_font_path = (fonts["italic_path"] or fonts["serif_path"]) if template == "D" else fonts["serif_path"]
    fit_transform = _turkish_upper if template == "D" else None
    font_size, lines, fit_ok, reason, balance = _fit_text(
        text, max_text_width, max_font_size, fit_font_path, transform=fit_transform, tracked=(template == "D"),
    )
    if not fit_ok:
        return RenderResult(img, template, font_size, lines, False, reason, line_length_balance=balance)

    font = _load_font(fonts["serif_path"], font_size)
    tracked_font = _load_font(fonts["italic_path"] if template == "D" else fonts["serif_path"], font_size)
    italic_font = _load_font(fonts["italic_path"] or fonts["serif_path"], max(26, int(font_size * 0.42)))
    draw = ImageDraw.Draw(img)
    line_heights = [
        draw.textbbox((0, 0), (_turkish_upper(ln) if template == "D" else ln), font=(tracked_font if template == "D" else font))[3]
        for ln in lines
    ]
    line_gap = int(font_size * 0.35)
    block_height = sum(line_heights) + line_gap * (len(lines) - 1)
    extra_bottom = (max(26, int(font_size * 0.42)) + 25) if author else 0

    top, zone, fallback_used, placement_score = _choose_zone(img, template, w, h, block_height, extra_bottom)

    # Point 1, independent safety net: TEXT OVERFLOW = HARD FAIL. _fit_text
    # already guarantees the wrapped width fits max_text_width under the
    # exact draw-time transform/tracking, and _zone_box already clamps top
    # into [SAFE_MARGIN, h-SAFE_MARGIN-total_h] -- but re-verify explicitly
    # here, on the actual font/tracking about to be used, rather than trust
    # that invariant blindly. A single letter past any of the four safe
    # margins must reject the render, never publish a broken layout.
    if template == "D":
        _tracking = max(2, font_size // 22)
        _measure = lambda ln: _tracked_text_width(draw, _turkish_upper(ln), tracked_font, _tracking)
    else:
        _measure = lambda ln: draw.textbbox((0, 0), ln, font=font)[2]
    max_line_width = max((_measure(ln) for ln in lines), default=0)
    horizontal_overflow = max_line_width > max_text_width
    vertical_overflow = top < SAFE_MARGIN or (top + block_height + extra_bottom) > (h - SAFE_MARGIN)
    if horizontal_overflow or vertical_overflow:
        return RenderResult(
            img, template, font_size, lines, False,
            "Metin g\u00FCvenli kenar bo\u015Flu\u011Funu (safe margin, min 90px) ihlal ediyor -- HARD FAIL.",
            line_length_balance=balance, zone=zone, zone_fallback_used=fallback_used, placement_score=placement_score,
        )

    if template == "A":
        text_box = (SAFE_MARGIN, top - 20, w - SAFE_MARGIN, top + block_height + extra_bottom + 60)
        img = _apply_overlay(img, text_box, 0.20)
        bg_luminance = _percentile_brightness(img, text_box)
        draw = ImageDraw.Draw(img)
        quote_mark_font = _load_font(fonts["serif_path"], int(font_size * 0.85))
        quote_mark_y = max(SAFE_MARGIN, top - int(font_size * 0.62))
        draw.text((SAFE_MARGIN, quote_mark_y), "\u201C", font=quote_mark_font, fill=COLOR_ACCENT)
        y = top
        for ln, lh in zip(lines, line_heights):
            draw.text((SAFE_MARGIN, y), ln, font=font, fill=COLOR_PRIMARY)
            y += lh + line_gap
        draw.line([(SAFE_MARGIN, y + 6), (SAFE_MARGIN + 70, y + 6)], fill=COLOR_ACCENT, width=2)
        if author:
            draw.text((SAFE_MARGIN, y + 26), author, font=italic_font, fill=COLOR_SECONDARY)

    elif template == "B":
        text_box = (SAFE_MARGIN, top - 40, w - SAFE_MARGIN, top + block_height + extra_bottom + 40)
        img = _apply_overlay(img, text_box, 0.22)
        bg_luminance = _percentile_brightness(img, text_box)
        draw = ImageDraw.Draw(img)
        y = top
        for ln, lh in zip(lines, line_heights):
            lw = draw.textbbox((0, 0), ln, font=font)[2]
            draw.text(((w - lw) // 2, y), ln, font=font, fill=COLOR_PRIMARY)
            y += lh + line_gap
        if author:
            att = f"— {author}"
            aw = draw.textbbox((0, 0), att, font=italic_font)[2]
            draw.text(((w - aw) // 2, y + 20), att, font=italic_font, fill=COLOR_SECONDARY)

    elif template == "C":
        text_box = (0, top - 60, w, min(h, top + block_height + extra_bottom + 60))
        img = _apply_overlay(img, text_box, 0.32)
        bg_luminance = _percentile_brightness(img, text_box)
        draw = ImageDraw.Draw(img)
        y = top
        for ln, lh in zip(lines, line_heights):
            draw.text((SAFE_MARGIN, y), ln, font=font, fill=COLOR_PRIMARY)
            y += lh + line_gap
        if author:
            draw.text((SAFE_MARGIN, y + 15), author, font=italic_font, fill=COLOR_SECONDARY)

    else:  # D -- dark image, wide-tracked premium serif
        text_box = (0, top - 60, w, top + block_height + extra_bottom + 60)
        img = _apply_overlay(img, text_box, 0.30)
        bg_luminance = _percentile_brightness(img, text_box)
        draw = ImageDraw.Draw(img)
        tracking = max(2, font_size // 22)
        y = top
        for ln, lh in zip(lines, line_heights):
            # Always uppercase here, unconditionally -- matching exactly the
            # transform _fit_text used to measure this line. Applying it
            # only to "short enough" lines (the old behavior) let a line
            # that fit in mixed case silently grow past the frame once
            # uppercased at draw time.
            _draw_tracked_text(draw, (SAFE_MARGIN, y), _turkish_upper(ln), tracked_font, COLOR_PRIMARY, tracking, anchor_center_width=max_text_width)
            y += lh + line_gap
        if author:
            att_w = draw.textbbox((0, 0), author, font=italic_font)[2]
            draw.text(((w - att_w) // 2, y + 25), author, font=italic_font, fill=COLOR_SECONDARY)

    if handle:
        handle_font = _load_font(fonts["italic_path"] or fonts["serif_path"], 24)
        draw = ImageDraw.Draw(img)
        hw = draw.textbbox((0, 0), handle, font=handle_font)[2]
        draw.text((w - SAFE_MARGIN - hw, h - SAFE_MARGIN + 10), handle, font=handle_font, fill=COLOR_SECONDARY)

    return RenderResult(
        img, template, font_size, lines, True, None,
        bg_luminance_at_text=bg_luminance, zone=zone, zone_fallback_used=fallback_used,
        placement_score=placement_score, line_length_balance=balance,
    )


_NINE_ZONES = [
    "top_left", "top_center", "top_right",
    "center_left", "center", "center_right",
    "bottom_left", "bottom_center", "bottom_right",
]
_ZONE_AXES = {
    "top_left": ("top", "left"), "top_center": ("top", "center"), "top_right": ("top", "right"),
    "center_left": ("center", "left"), "center": ("center", "center"), "center_right": ("center", "right"),
    "bottom_left": ("bottom", "left"), "bottom_center": ("bottom", "center"), "bottom_right": ("bottom", "right"),
}
# Rule-of-thirds bias: no semantic segmentation is available to detect the
# actual subject or horizon line (documented limitation), so a fixed
# centrality penalty is the practical stand-in for "don't cover the focal
# point" -- a merely-clean center cell should lose to an equally-clean edge
# cell, and a truly busy center cell should lose even harder.
_ZONE_CENTRALITY_PENALTY = {
    "top_left": 0.0, "top_center": 0.03, "top_right": 0.0,
    "center_left": 0.05, "center": 0.12, "center_right": 0.05,
    "bottom_left": 0.0, "bottom_center": 0.03, "bottom_right": 0.0,
}


def _nine_zone_position(zone: str, w: int, h: int, block_w: int, block_h: int) -> tuple[int, int, tuple[int, int, int, int]]:
    """Returns (x, y, scoring_box) -- top-left corner to draw a block_w x
    block_h text block at for one of the 9 grid cells, and a padded box
    around it for brightness/edge scoring. x/y are always clamped so the
    block never crosses SAFE_MARGIN on any edge, even if block_w/block_h is
    larger than that cell's own third of the canvas."""
    v_pref, h_pref = _ZONE_AXES[zone]
    top_edge = TOP_VISUAL_MARGIN if v_pref == "top" else SAFE_MARGIN
    bottom_edge = h - BOTTOM_VISUAL_MARGIN if v_pref == "bottom" else h - SAFE_MARGIN
    x_lo, x_hi = {"left": (SAFE_MARGIN, w // 3), "center": (w // 3, 2 * w // 3), "right": (2 * w // 3, w - SAFE_MARGIN)}[h_pref]
    y_lo, y_hi = {"top": (top_edge, h // 3), "center": (h // 3, 2 * h // 3), "bottom": (2 * h // 3, bottom_edge)}[v_pref]

    if h_pref == "left":
        x = x_lo
    elif h_pref == "right":
        x = x_hi - block_w
    else:
        x = (x_lo + x_hi - block_w) // 2
    if v_pref == "top":
        y = y_lo
    elif v_pref == "bottom":
        y = y_hi - block_h
    else:
        y = (y_lo + y_hi - block_h) // 2

    x = max(SAFE_MARGIN, min(x, w - SAFE_MARGIN - block_w))
    y = max(top_edge, min(y, bottom_edge - block_h))
    pad = 50
    box = (max(0, x - pad), max(0, y - pad), min(w, x + block_w + pad), min(h, y + block_h + pad))
    return x, y, box


def _choose_nine_zone(image: Image.Image, w: int, h: int, block_w: int, block_h: int,
                       recent_zones: list[str] | None = None, force_zone: str | None = None) -> tuple[int, int, str, bool, float]:
    """Scores all 9 grid cells for cleanliness (brightness + edge density +
    centrality penalty) and picks the best one, EXCEPT a cell that was
    already used in each of the last 2 posts (recent_zones[0] and [1],
    most-recent-first) -- "aynı placement art arda maksimum 2 postta
    kullanılabilsin". Returns (x, y, zone_name, repetition_avoided,
    placement_score [0..1, higher=cleaner]).

    force_zone (point 4, grid-wide balance): overrides the automatic pick
    with a SPECIFIC zone -- used only when a human (or the caller doing a
    grid-balance pass) has already checked that this zone scores close to
    the automatic winner, so quality isn't being traded away just for
    variety's sake. placement_score is still computed honestly for that
    exact zone, not faked."""
    recent_zones = recent_zones or []
    blocked_zone = recent_zones[0] if (len(recent_zones) >= 2 and recent_zones[0] == recent_zones[1]) else None

    scored = []
    for zone in _NINE_ZONES:
        x, y, box = _nine_zone_position(zone, w, h, block_w, block_h)
        raw_score, _, _ = _zone_score(image, box)
        scored.append((zone, x, y, raw_score + _ZONE_CENTRALITY_PENALTY[zone]))

    if force_zone is not None:
        chosen = next(s for s in scored if s[0] == force_zone)
        return chosen[1], chosen[2], chosen[0], False, max(0.0, 1.0 - chosen[3])

    naive_best = min(scored, key=lambda t: t[3])
    eligible = [s for s in scored if s[0] != blocked_zone] or scored
    best_zone, best_x, best_y, best_score = min(eligible, key=lambda t: t[3])
    repetition_avoided = blocked_zone is not None and naive_best[0] == blocked_zone and best_zone != blocked_zone

    placement_score = max(0.0, 1.0 - best_score)
    return best_x, best_y, best_zone, repetition_avoided, placement_score


def render_quote_editorial(
    background: Image.Image,
    text: str,
    author: str | None = None,
    handle: str | None = None,
    recent_zones: list[str] | None = None,
    *,
    font_scale: float = 1.0,
    vertical_shift_frac: float = 0.0,
    horizontal_shift_px: int = 0,
    underline_length: int = 70,
    underline_width: int = 2,
    show_quote_mark: bool | None = None,
    show_underline: bool | None = None,
    force_zone: str | None = None,
) -> RenderResult:
    """The unified brand template (module docstring's "UNIFIED BRAND
    TEMPLATE"): elegant normal-weight serif, white/off-white text, a small
    restrained quote mark and short thin underline shown only some of the
    time, minimal uppercase/italics. Placement is chosen per-image from 9
    rule-of-thirds cells by _choose_nine_zone() -- never fixed.

    font_scale/vertical_shift_frac/horizontal_shift_px/underline_length/
    underline_width/show_quote_mark/show_underline are per-post fine-tuning
    knobs (used e.g. to nudge one specific post's layout, or pull it further
    from an edge than the automatic zone position already does) layered on
    top of the same algorithm everyone else uses -- they do not change the
    placement logic itself, and are still re-clamped to SAFE_MARGIN
    afterward. show_quote_mark/show_underline default to a deterministic
    hash of the quote text when not given explicitly, so typography varies
    quote-to-quote (roughly 60% of quotes get a quote mark, 60% get an
    underline, independently) but a given quote always renders identically."""
    import hashlib

    fonts = check_font_availability()
    img = background.convert("RGB")
    img = _smart_crop_to_ratio(img, CANVAS_SIZE)
    w, h = img.size
    max_text_width = w - 2 * SAFE_MARGIN

    max_font_size = max(MIN_FONT_SIZE, int(68 * font_scale))
    font_size, lines, fit_ok, reason, balance = _fit_text(text, max_text_width, max_font_size, fonts["serif_path"])
    if not fit_ok:
        return RenderResult(img, "EDITORIAL", font_size, lines, False, reason, line_length_balance=balance)

    qhash = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    if show_quote_mark is None:
        show_quote_mark = (qhash % 5) < 3
    if show_underline is None:
        show_underline = ((qhash // 5) % 5) < 3

    font = _load_font(fonts["serif_path"], font_size)
    italic_font = _load_font(fonts["italic_path"] or fonts["serif_path"], max(24, int(font_size * 0.40)))
    draw = ImageDraw.Draw(img)
    line_widths = [draw.textbbox((0, 0), ln, font=font)[2] for ln in lines]
    line_heights = [draw.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    line_gap = int(font_size * 0.35)
    block_width = max(line_widths, default=0)
    block_height = sum(line_heights) + line_gap * (len(lines) - 1)

    quote_mark_extra_top = int(font_size * 0.68) if show_quote_mark else 0
    underline_extra = (14 + underline_width) if show_underline else 0
    author_extra = (max(24, int(font_size * 0.40)) + 22) if author else 0
    extra_bottom = underline_extra + author_extra
    total_block_h = quote_mark_extra_top + block_height + extra_bottom

    x, y, zone, repetition_avoided, placement_score = _choose_nine_zone(img, w, h, block_width, total_block_h, recent_zones, force_zone)
    v_pref, h_pref = _ZONE_AXES[zone]
    top_limit = TOP_VISUAL_MARGIN if v_pref == "top" else SAFE_MARGIN
    bottom_limit = (h - BOTTOM_VISUAL_MARGIN) if v_pref == "bottom" else (h - SAFE_MARGIN)
    top = y + quote_mark_extra_top
    if vertical_shift_frac:
        top -= int(h * vertical_shift_frac)
    top = max(top_limit + quote_mark_extra_top, min(top, bottom_limit - block_height - extra_bottom))
    if horizontal_shift_px:
        x = max(SAFE_MARGIN, min(x + horizontal_shift_px, w - SAFE_MARGIN - block_width))

    # Point 1, independent safety net (same policy as render_quote): a
    # single letter past any safe margin must reject the render outright.
    # Top/bottom-row placements are additionally held to TOP_VISUAL_MARGIN/
    # BOTTOM_VISUAL_MARGIN, not just the technical SAFE_MARGIN -- "safe
    # margin teknik olarak geçse bile metni kadraja sıkıştırma".
    horiz_ok = x >= SAFE_MARGIN and x + block_width <= w - SAFE_MARGIN
    vert_ok = (top - quote_mark_extra_top) >= top_limit and (top + block_height + extra_bottom) <= bottom_limit
    if not (horiz_ok and vert_ok):
        return RenderResult(
            img, "EDITORIAL", font_size, lines, False,
            "Metin güvenli kenar boşluğunu (safe margin, min 90px) ihlal ediyor -- HARD FAIL.",
            line_length_balance=balance, zone=zone, zone_fallback_used=repetition_avoided, placement_score=placement_score,
        )

    text_box = (
        max(0, x - 50), max(0, top - quote_mark_extra_top - 50),
        min(w, x + block_width + 50), min(h, top + block_height + extra_bottom + 50),
    )
    img = _apply_overlay(img, text_box, 0.18)
    bg_luminance = _percentile_brightness(img, text_box)
    draw = ImageDraw.Draw(img)

    def _align_x(item_w: int) -> int:
        if h_pref == "right":
            return x + block_width - item_w
        if h_pref == "center":
            return x + (block_width - item_w) // 2
        return x

    if show_quote_mark:
        qsize = int(font_size * 0.62)  # kept small/minimal, point 5's "küçük ve zarif quote işareti"
        quote_font = _load_font(fonts["serif_path"], qsize)
        qbbox = draw.textbbox((0, 0), "“", font=quote_font)
        qy = max(SAFE_MARGIN, top - int(font_size * 0.58))
        draw.text((_align_x(qbbox[2] - qbbox[0]), qy), "“", font=quote_font, fill=COLOR_ACCENT)

    yy = top
    for ln, lw, lh in zip(lines, line_widths, line_heights):
        draw.text((_align_x(lw), yy), ln, font=font, fill=COLOR_PRIMARY)
        yy += lh + line_gap

    if show_underline:
        uy = yy + 8
        ux0 = _align_x(underline_length)
        draw.line([(ux0, uy), (ux0 + underline_length, uy)], fill=COLOR_ACCENT, width=underline_width)
        yy = uy + underline_width + 10

    if author:
        att_w = draw.textbbox((0, 0), author, font=italic_font)[2]
        draw.text((_align_x(att_w), yy + 14), author, font=italic_font, fill=COLOR_SECONDARY)

    if handle:
        handle_font = _load_font(fonts["italic_path"] or fonts["serif_path"], 24)
        hw = draw.textbbox((0, 0), handle, font=handle_font)[2]
        draw.text((w - SAFE_MARGIN - hw, h - SAFE_MARGIN + 10), handle, font=handle_font, fill=COLOR_SECONDARY)

    return RenderResult(
        img, "EDITORIAL", font_size, lines, True, None,
        bg_luminance_at_text=bg_luminance, zone=zone, zone_fallback_used=repetition_avoided,
        placement_score=placement_score, line_length_balance=balance,
        text_top=top - quote_mark_extra_top, text_bottom=top + block_height + extra_bottom,
        text_left=x, text_right=x + block_width,
    )


# ---------------------------------------------------------------------------
# STORY renderer (point 6 of the 2026-09-01 automation build): same brand
# identity as render_quote_editorial (serif, off-white, optional quote mark/
# thin underline) but on a 1080x1920 (9:16) canvas, deliberately NOT sharing
# render_quote_editorial's 9-zone/SAFE_MARGIN machinery -- Instagram's own UI
# reserves large top/bottom bands on a story (profile bar + close button up
# top, reply field + sticker tray at the bottom) that don't exist on a feed
# post, so a simpler 3-band (upper/middle/lower) placement scored with the
# same generic _zone_score()/_percentile_brightness() measurement is used
# instead, explicitly excluding those UI bands rather than the feed's
# smaller universal safe margin.
# ---------------------------------------------------------------------------
STORY_CANVAS_SIZE = (1080, 1920)
STORY_SIDE_MARGIN = 80
STORY_TOP_SAFE = 260     # Instagram UI: profile pic, username, close (X), progress bar
STORY_BOTTOM_SAFE = 260  # Instagram UI: reply field, sticker tray
STORY_MAX_LINES = 2      # point 6: "Story yazıları genelde daha kısa olsun"


def _story_zone_box(v_zone: str, w: int, h: int, block_h: int) -> tuple[int, tuple[int, int, int, int]]:
    usable_top, usable_bottom = STORY_TOP_SAFE, h - STORY_BOTTOM_SAFE
    band_h = (usable_bottom - usable_top) // 3
    candidate_top = {
        "upper": usable_top,
        "middle": usable_top + band_h + (band_h - block_h) // 2,
        "lower": usable_bottom - block_h,
    }[v_zone]
    top = max(usable_top, min(candidate_top, usable_bottom - block_h))
    pad = 60
    box = (0, max(0, top - pad), w, min(h, top + block_h + pad))
    return top, box


def _choose_story_zone(image: Image.Image, w: int, h: int, block_h: int,
                        recent_zones: list[str] | None = None) -> tuple[int, str, bool, float]:
    recent_zones = recent_zones or []
    blocked = recent_zones[0] if (len(recent_zones) >= 2 and recent_zones[0] == recent_zones[1]) else None
    scored = []
    for zone in ("upper", "middle", "lower"):
        top, box = _story_zone_box(zone, w, h, block_h)
        raw_score, _, _ = _zone_score(image, box)
        centrality_penalty = 0.06 if zone == "middle" else 0.0  # same rationale as _ZONE_CENTRALITY_PENALTY
        scored.append((zone, top, raw_score + centrality_penalty))
    naive_best = min(scored, key=lambda t: t[2])
    eligible = [s for s in scored if s[0] != blocked] or scored
    best_zone, best_top, best_score = min(eligible, key=lambda t: t[2])
    repetition_avoided = blocked is not None and naive_best[0] == blocked and best_zone != blocked
    return best_top, best_zone, repetition_avoided, max(0.0, 1.0 - best_score)


def render_quote_story(
    background: Image.Image,
    text: str,
    author: str | None = None,
    recent_zones: list[str] | None = None,
    *,
    font_scale: float = 1.0,
    show_quote_mark: bool | None = None,
    show_underline: bool | None = None,
) -> RenderResult:
    """Story variant of the unified brand template. Same visual family as
    render_quote_editorial (premium serif, off-white text, restrained quote
    mark/underline sometimes) so the account reads as one brand across feed
    and story, but not the identical template -- centered placement, a
    3-band vertical search instead of 9, max 2 lines, and a hard UI
    safe-zone instead of the feed's SAFE_MARGIN."""
    import hashlib

    fonts = check_font_availability()
    img = background.convert("RGB")
    img = _smart_crop_to_ratio(img, STORY_CANVAS_SIZE)
    w, h = img.size
    max_text_width = w - 2 * STORY_SIDE_MARGIN

    max_font_size = max(MIN_FONT_SIZE, int(60 * font_scale))
    font_size, lines, fit_ok, reason, balance = _fit_text(
        text, max_text_width, max_font_size, fonts["serif_path"], max_lines=STORY_MAX_LINES,
    )
    if not fit_ok:
        return RenderResult(img, "STORY", font_size, lines, False, reason, line_length_balance=balance)

    qhash = int(hashlib.sha256(text.encode("utf-8")).hexdigest(), 16)
    if show_quote_mark is None:
        show_quote_mark = (qhash % 5) < 2  # stories lean plainer than feed -- rarer quote mark
    if show_underline is None:
        show_underline = ((qhash // 5) % 5) < 2

    font = _load_font(fonts["serif_path"], font_size)
    italic_font = _load_font(fonts["italic_path"] or fonts["serif_path"], max(22, int(font_size * 0.38)))
    draw = ImageDraw.Draw(img)
    line_widths = [draw.textbbox((0, 0), ln, font=font)[2] for ln in lines]
    line_heights = [draw.textbbox((0, 0), ln, font=font)[3] for ln in lines]
    line_gap = int(font_size * 0.35)
    block_width = max(line_widths, default=0)
    block_height = sum(line_heights) + line_gap * (len(lines) - 1)

    quote_mark_extra_top = int(font_size * 0.62) if show_quote_mark else 0
    underline_extra = (12 + 2) if show_underline else 0
    author_extra = (max(22, int(font_size * 0.38)) + 20) if author else 0
    extra_bottom = underline_extra + author_extra
    total_block_h = quote_mark_extra_top + block_height + extra_bottom

    top_y, zone, repetition_avoided, placement_score = _choose_story_zone(img, w, h, total_block_h, recent_zones)
    top = top_y + quote_mark_extra_top
    x = (w - block_width) // 2  # centered -- simplest, most reliable default for short story text

    horiz_ok = x >= STORY_SIDE_MARGIN and x + block_width <= w - STORY_SIDE_MARGIN
    vert_ok = (top - quote_mark_extra_top) >= STORY_TOP_SAFE and (top + block_height + extra_bottom) <= h - STORY_BOTTOM_SAFE
    if not (horiz_ok and vert_ok):
        return RenderResult(
            img, "STORY", font_size, lines, False,
            "Metin story safe-zone'unu (üst/alt Instagram UI alanı) ihlal ediyor -- HARD FAIL.",
            line_length_balance=balance, zone=zone, zone_fallback_used=repetition_avoided, placement_score=placement_score,
        )

    text_box = (
        max(0, x - 50), max(0, top - quote_mark_extra_top - 50),
        min(w, x + block_width + 50), min(h, top + block_height + extra_bottom + 50),
    )
    img = _apply_overlay(img, text_box, 0.20)
    bg_luminance = _percentile_brightness(img, text_box)
    draw = ImageDraw.Draw(img)

    if show_quote_mark:
        qsize = int(font_size * 0.58)
        quote_font = _load_font(fonts["serif_path"], qsize)
        qbbox = draw.textbbox((0, 0), "“", font=quote_font)
        qx = x + (block_width - (qbbox[2] - qbbox[0])) // 2
        qy = max(STORY_TOP_SAFE, top - int(font_size * 0.54))
        draw.text((qx, qy), "“", font=quote_font, fill=COLOR_ACCENT)

    yy = top
    for ln, lw, lh in zip(lines, line_widths, line_heights):
        lx = x + (block_width - lw) // 2
        draw.text((lx, yy), ln, font=font, fill=COLOR_PRIMARY)
        yy += lh + line_gap

    if show_underline:
        uy = yy + 6
        ul = min(60, block_width)
        ux0 = x + (block_width - ul) // 2
        draw.line([(ux0, uy), (ux0 + ul, uy)], fill=COLOR_ACCENT, width=2)
        yy = uy + 2 + 8

    if author:
        att_w = draw.textbbox((0, 0), author, font=italic_font)[2]
        draw.text((x + (block_width - att_w) // 2, yy + 12), author, font=italic_font, fill=COLOR_SECONDARY)

    return RenderResult(
        img, "STORY", font_size, lines, True, None,
        bg_luminance_at_text=bg_luminance, zone=zone, zone_fallback_used=repetition_avoided,
        placement_score=placement_score, line_length_balance=balance,
        text_top=top - quote_mark_extra_top, text_bottom=top + block_height + extra_bottom,
        text_left=x, text_right=x + block_width,
    )


def _smart_crop_to_ratio(image: Image.Image, target_size: tuple[int, int]) -> Image.Image:
    """Center-crops to the target aspect ratio, then resizes. Center-crop is
    a deliberately simple, dependency-free heuristic (no saliency model);
    scenes are composed with the subject roughly centered so this holds up
    in practice."""
    tw, th = target_size
    target_ratio = tw / th
    w, h = image.size
    current_ratio = w / h
    if current_ratio > target_ratio:
        new_w = int(h * target_ratio)
        left = (w - new_w) // 2
        image = image.crop((left, 0, left + new_w, h))
    elif current_ratio < target_ratio:
        new_h = int(w / target_ratio)
        top = (h - new_h) // 2
        image = image.crop((0, top, w, top + new_h))
    return image.resize(target_size, Image.LANCZOS)
