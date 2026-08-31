"""Local, offline content-idea bank: caption fragments, hashtag pools and AI
image-prompt templates per theme. No LLM API call involved -- this is a
template-and-rotation generator, deliberately kept simple and free.

Rationale (documented since it's a real design choice): fully free-form,
always-fresh Turkish captions would need an LLM API call per post, which means
another paid key and another failure point in a headless GitHub Actions job.
Instead this bank holds enough hand-written variety per theme, and
content_quality.py rejects near-duplicates against content_history.json so the
same fragment/hashtag-set combination doesn't repeat too often. If richer
variation is wanted later, swap generate_caption()/generate_image_prompt() for
a real LLM call -- everything downstream (queue schema, quality gate, planner)
already treats caption/image_prompt as opaque strings.

Content strategy (2026-08-31, user-directed): a "premium ama gerçek" lifestyle
account feel, not "an account that posts random AI images". Five themes:
travel/city/landscape, men's style/watches/accessories, everyday
lifestyle/coffee/places, automotive/road-trip atmosphere, and occasional
creative concept shots. Distribution is a *soft* weighting (pick_theme_for_slot
never repeats the immediately preceding theme, and performance data can nudge
it a little -- see _effective_weights below) -- it will not match the target
percentages exactly every single week, only on average over time.
"""
import json
import random
from pathlib import Path

THEMES = ["travel_landscape", "style_fashion", "lifestyle", "automotive", "creative_concept"]

THEME_WEIGHTS = {
    "travel_landscape": 0.35,
    "style_fashion": 0.25,
    "lifestyle": 0.20,
    "automotive": 0.10,
    "creative_concept": 0.10,
}

STRATEGY_WEIGHTS_PATH = Path(__file__).resolve().parent.parent / "strategy_weights.json"
# How far a theme's weight is allowed to drift from its base value based on
# performance data -- keeps "boost what works" from turning into "only ever
# post one theme" (explicit user instruction).
MAX_PERFORMANCE_ADJUSTMENT = 0.20  # +/-20% relative to the theme's base weight
MIN_SAMPLES_TO_TRUST = 5

# Phrases considered tired/AI-influencer-sounding cliches. content_bank never
# emits these; content_quality.py also scans for them in case a caption is
# ever supplied by hand (e.g. via scripts/add_to_queue.py) so the rule is
# enforced consistently regardless of source.
CLICHE_PHRASES = [
    "hayallerinin peşinden git", "hayatını yaşa", "başarı bir yolculuktur",
    "asla pes etme", "sınırlarını zorla", "en iyi versiyonun",
    "pozitif enerji", "kendine inan", "hayat kısa", "an'ı yaşa",
    "dream big", "chase your dreams", "believe in yourself", "good vibes only",
    "hustle", "grind mode", "mindset is everything", "unutulmaz bir gün",
    "her an bir hediye", "kalbinin sesini dinle",
]

# Generic engagement-bait tags never used regardless of theme.
SPAM_HASHTAGS = {"#viral", "#fyp", "#explorepage", "#follow", "#followme", "#like4like", "#instagood"}

CAPTION_BANK = {
    "travel_landscape": [
        "Buraya bir süre önce takıldım, hâlâ aklımda.",
        "Yol biraz uzundu ama manzara buna değdi.",
        "Bazı yerler fotoğrafta bile eksik kalıyor, burası öyle.",
        "Şehirden biraz uzaklaşmak iyi geldi.",
        "Bu ışığı yakalamak için biraz beklemek gerekti.",
        "Haritada küçük bir nokta, gerçekte hiç öyle değil.",
        "Buraya çıkmak biraz vakit aldı ama son 10 dakika manzara için yeterliydi. "
        "Telefon çekmiyordu, belki de daha iyi oldu.",
        "Bu şehri üçüncü kez görüyorum ama her seferinde farklı bir köşesini keşfediyorum. "
        "Bugünkü keşif bu taraftaydı.",
    ],
    "style_fashion": [
        "Bugünkü kombin böyle şekillendi.",
        "Basit ama üstünde duruyor.",
        "Sonbahar dolabı yavaş yavaş çıkıyor.",
        "Bazen az şey, çok şey ifade ediyor.",
        "Bu renk uzun zamandır dolapta bekliyordu.",
        "Saat birkaç yıllık ama hâlâ favorim.",
        "Bugün üşenmeden kombin yapmak istedim, sonuç fena olmadı.",
        "Bu ceketi alalı epey oldu ama tam bugünlük bir hava vardı, denemek istedim.",
    ],
    "lifestyle": [
        "Bugün işler biraz yavaşladı, iyi de oldu.",
        "Kahve, biraz sessizlik, bir de bu manzara.",
        "Haftanın ortasında küçük bir mola.",
        "Bazı günler planlı değil, öyle daha iyi.",
        "Basit bir gün, basit bir kare.",
        "Elimde kahve, kafamda hiçbir şey yok -- tam da istediğim gibi.",
        "Bugünü sadece kaydetmek istedim.",
        "Bu mekana bir işim için uğramıştım, kahvesi o kadar iyiydi ki fotoğrafını çekmeden duramadım.",
        "Sabahın bu saatinde burası hâlâ sakin. Bir süre daha böyle kalsın istedim.",
    ],
    "automotive": [
        "Yol biraz uzun sürdü ama hiç sıkılmadım.",
        "Direksiyon, biraz müzik, uzun bir yol.",
        "Arada böyle molalar iyi geliyor.",
        "Bazı yolculuklar varış noktasından çok yolun kendisiyle ilgili, bu da onlardan biriydi.",
        "Bu yolu daha önce hiç bu saatte sürmemiştim, farklı bir hali varmış.",
        "Uzun yol iyi gelir bazen -- düşünmeden sadece sürmek.",
    ],
    "creative_concept": [
        "Bugün biraz farklı bir kare denemek istedim.",
        "Basit şeyleri yan yana koyunca bazen iyi bir kare çıkıyor.",
        "Bu ikisini yan yana görünce fotoğrafını çekmem gerekti.",
        "Bazen sahne kurmak, anı yakalamaktan daha eğlenceli.",
        "Bu kareyi kurmak biraz zaman aldı ama denemeye değdi. Basit ama sevdim.",
        "Aklımda bir süredir bu kare vardı, bugün denedim.",
    ],
}

HASHTAG_POOL = {
    "travel_landscape": ["#seyahat", "#manzara", "#doğa", "#gezi", "#türkiye",
                          "#günbatımı", "#yolculuk", "#doğayla", "#keşfet", "#şehir"],
    "style_fashion": ["#stil", "#kombin", "#günlükstil", "#moda", "#giyim",
                       "#erkekstili", "#sonbaharstili", "#basitşıklık", "#saat", "#aksesuar"],
    "lifestyle": ["#günlükhayat", "#sadelik", "#anlar", "#gündelik", "#huzur",
                  "#kahvekeyfi", "#yavaşyaşam", "#bugün", "#keyifanı", "#mekan"],
    "automotive": ["#otomobil", "#yolculuk", "#roadtrip", "#arabatutkusu",
                   "#yolda", "#direksiyon", "#gezitutkusu"],
    "creative_concept": ["#kompozisyon", "#detay", "#estetik", "#kareyakalamak",
                          "#minimal", "#yaratıcı", "#sahne"],
}

# Shared negative/anti-AI-look qualifiers appended to every generated prompt --
# this is what keeps output from reading as "obviously AI" (overly perfect,
# glossy, symmetrical, fantastical).
_REALISM_SUFFIX = (
    ", natural imperfections and realistic texture, subtle film-like grain, "
    "true-to-camera color grading (not oversaturated), realistic amateur or "
    "prosumer photography look, NOT a glossy CGI/3D render, NOT surreal or "
    "fantastical, no perfectly symmetrical composition, absolutely no visible "
    "readable text, no handwriting, no illegible scribbles, no signage with "
    "text, no logos, no watermark -- if a notebook/page/sign appears in "
    "frame it must be blank or out of focus, not carry any lettering"
)

IMAGE_PROMPT_TEMPLATES = {
    "lifestyle": [
        "Candid lifestyle photo, over-the-shoulder or hands-only framing (no "
        "clearly visible identifiable face), person holding a coffee cup at a "
        "wooden cafe table by a window, soft natural daylight, shallow depth "
        "of field, warm color grading" + _REALISM_SUFFIX,
        "Candid lifestyle detail shot: a wooden desk with a closed notebook, a "
        "cup of tea and sunlight coming through a window, cozy home interior, "
        "natural shadows, no visible faces" + _REALISM_SUFFIX,
    ],
    "travel_landscape": [
        "Realistic landscape photograph of a calm lake surrounded by forested "
        "hills at golden hour, natural warm sunlight, soft clouds, believable "
        "real-world location, no people" + _REALISM_SUFFIX,
        "Realistic mountain valley view at sunrise, layered mist between "
        "ridgelines, natural muted color palette, shot on a wide-angle lens, "
        "no people" + _REALISM_SUFFIX,
        "Realistic candid street photo of a quiet old-town alley in early "
        "morning light, warm stone buildings, no people, believable travel "
        "snapshot" + _REALISM_SUFFIX,
    ],
    "style_fashion": [
        "Editorial fashion photo, person from the neck down or from behind "
        "wearing a clean minimalist outfit (jacket, plain t-shirt, trousers), "
        "walking on an urban street, natural daylight, shallow depth of "
        "field, correct human anatomy, no visible identifiable face" + _REALISM_SUFFIX,
        "Flat-lay style fashion photo of a neatly arranged outfit (jacket, "
        "shirt, watch, shoes) on a clean neutral background, soft natural "
        "window light" + _REALISM_SUFFIX,
        "Close-up detail photo of a wristwatch on a wrist resting on a table "
        "next to a coffee cup, soft natural light, shallow depth of field, no "
        "visible identifiable face" + _REALISM_SUFFIX,
    ],
    "automotive": [
        "Realistic photo of a car interior/dashboard detail during a road "
        "trip, hands on the steering wheel wearing a wristwatch (no visible "
        "face), warm afternoon light through the windshield, natural travel "
        "photography look" + _REALISM_SUFFIX,
        "Realistic photo of a car parked by a scenic overlook road at dusk, "
        "taillights on, mountains in the background, no people visible, "
        "believable travel-photography color grading" + _REALISM_SUFFIX,
    ],
    "creative_concept": [
        "Creative but realistic top-down still-life composition: a camera, a "
        "passport, a cup of coffee and sunglasses arranged on a rustic wooden "
        "table, soft natural window light, minimal aesthetic styling" + _REALISM_SUFFIX,
        "Realistic long-exposure-style photo of city lights and light trails "
        "at night from a rooftop, moody but true-to-camera color grading, no "
        "people" + _REALISM_SUFFIX,
    ],
}


def _load_strategy_weights() -> dict | None:
    if not STRATEGY_WEIGHTS_PATH.exists():
        return None
    try:
        with open(STRATEGY_WEIGHTS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None


def _effective_weights() -> dict[str, float]:
    """Base THEME_WEIGHTS, nudged (never overridden) by performance data --
    only for themes with enough samples, and never by more than
    MAX_PERFORMANCE_ADJUSTMENT relative to the theme's base weight, so a
    strong run of one theme biases the mix without ever taking it over."""
    weights = dict(THEME_WEIGHTS)
    strategy = _load_strategy_weights()
    by_theme = (strategy or {}).get("by_theme") or {}
    trusted = {t: d["avg_score"] for t, d in by_theme.items()
               if t in weights and d.get("n", 0) >= MIN_SAMPLES_TO_TRUST}
    if len(trusted) >= 2:
        mean_score = sum(trusted.values()) / len(trusted)
        if mean_score > 0:
            for theme, score in trusted.items():
                relative = (score - mean_score) / mean_score
                adjustment = max(-MAX_PERFORMANCE_ADJUSTMENT, min(MAX_PERFORMANCE_ADJUSTMENT, relative))
                weights[theme] = weights[theme] * (1 + adjustment)
    total = sum(weights.values())
    return {t: w / total for t, w in weights.items()}


def pick_theme_for_slot(recent_themes: list[str]) -> str:
    """Weighted random pick that avoids repeating the immediately preceding
    theme (the 'aynı temayı arka arkaya paylaşma' rule). Weights come from
    _effective_weights(), i.e. the base distribution lightly nudged by any
    trustworthy performance signal."""
    weights_map = _effective_weights()
    candidates = [t for t in THEMES if not recent_themes or t != recent_themes[-1]]
    weights = [weights_map[t] for t in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def generate_caption(theme: str, used_recently: set[str]) -> str:
    pool = [c for c in CAPTION_BANK[theme] if c not in used_recently]
    if not pool:
        pool = CAPTION_BANK[theme]
    return random.choice(pool)


def generate_hashtags(theme: str, used_sets_recently: list[list[str]], n: int | None = None) -> list[str]:
    """n defaults to a random 4-8 (per-post variety, per instruction), never
    includes SPAM_HASHTAGS, and avoids near-repeating the last set used for
    this theme."""
    n = n or random.randint(4, 8)
    pool = [h for h in HASHTAG_POOL[theme] if h not in SPAM_HASHTAGS]
    random.shuffle(pool)
    candidate = pool[:n]
    for prev in used_sets_recently:
        overlap = len(set(candidate) & set(prev))
        if prev and overlap / max(len(prev), 1) > 0.7:
            random.shuffle(pool)
            candidate = pool[:n]
    return candidate


def generate_image_prompt(theme: str) -> str:
    return random.choice(IMAGE_PROMPT_TEMPLATES[theme])
