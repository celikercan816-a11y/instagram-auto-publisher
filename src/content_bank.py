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
"""
import random

THEMES = ["lifestyle", "travel_landscape", "style_fashion", "motivation"]

# Weekly distribution the user specified. "reels" is a content_type, not a
# separate theme -- a reels slot still picks one of the four themes below.
THEME_WEIGHTS = {
    "lifestyle": 0.30,
    "travel_landscape": 0.25,
    "style_fashion": 0.20,
    "motivation": 0.15,
    # the remaining 0.10 is the reels slots, handled separately by the planner
}

# Phrases considered tired/AI-influencer-sounding cliches. content_bank never
# emits these; content_quality.py also scans for them in case a caption is
# ever supplied by hand (e.g. via scripts/add_to_queue.py) so the rule is
# enforced consistently regardless of source.
CLICHE_PHRASES = [
    "hayallerinin peşinden git", "hayatını yaşa", "başarı bir yolculuktur",
    "asla pes etme", "sınırlarını zorla", "en iyi versiyonun",
    "pozitif enerji", "kendine inan", "hayat kısa", "an'ı yaşa",
    "dream big", "chase your dreams", "believe in yourself", "good vibes only",
    "hustle", "grind mode", "mindset is everything",
]

CAPTION_BANK = {
    "lifestyle": [
        "Bugün işler biraz yavaşladı, iyi de oldu.",
        "Kahve, biraz sessizlik, bir de bu manzara.",
        "Haftanın ortasında küçük bir mola.",
        "Bazı günler planlı değil, öyle daha iyi.",
        "Basit bir gün, basit bir kare.",
        "Elimde kahve, kafamda hiçbir şey yok -- tam da istediğim gibi.",
        "Bugünü sadece kaydetmek istedim.",
    ],
    "travel_landscape": [
        "Buraya bir süre önce takıldım, hâlâ aklımda.",
        "Yol biraz uzundu ama manzara buna değdi.",
        "Bazı yerler fotoğrafta bile eksik kalıyor, burası öyle.",
        "Şehirden biraz uzaklaşmak iyi geldi.",
        "Bu ışığı yakalamak için biraz beklemek gerekti.",
        "Haritada küçük bir nokta, gerçekte hiç öyle değil.",
    ],
    "style_fashion": [
        "Bugünkü kombin böyle şekillendi.",
        "Basit ama üstünde duruyor.",
        "Sonbahar dolabı yavaş yavaş çıkıyor.",
        "Bazen az şey, çok şey ifade ediyor.",
        "Bu renk uzun zamandır dolapta bekliyordu.",
    ],
    "motivation": [
        "Bazı günler sadece bir şeyi bitirmek yeterli.",
        "Küçük ilerlemeler de sayılıyor, kendime hatırlatıyorum.",
        "Planın hepsi tutmadı ama gün yine de iyiydi.",
        "Yavaş gitmek de bir yöntem.",
        "Bugün öğrendiğim şey: bitirmek, mükemmel yapmaktan önemli.",
    ],
}

HASHTAG_POOL = {
    "lifestyle": ["#günlükhayat", "#sadelik", "#anlar", "#gündelik", "#huzur",
                  "#kahvekeyfi", "#yavaşyaşam", "#bugün", "#keyifanı"],
    "travel_landscape": ["#seyahat", "#manzara", "#doğa", "#gezi", "#türkiye",
                          "#günbatımı", "#yolculuk", "#doğayla", "#keşfet"],
    "style_fashion": ["#stil", "#kombin", "#günlükstil", "#moda", "#giyim",
                       "#erkekstili", "#sonbaharstili", "#basitşıklık"],
    "motivation": ["#düşünce", "#gündeminden", "#kendinehatırlat", "#ilerleme",
                   "#sadegünler", "#birazduraksa", "#bugünküders"],
}

IMAGE_PROMPT_TEMPLATES = {
    "lifestyle": [
        "Candid lifestyle photo, over-the-shoulder or hands-only framing (no "
        "clearly visible identifiable face), person holding a coffee cup at a "
        "wooden cafe table by a window, soft natural daylight, shallow depth "
        "of field, realistic phone-camera photo aesthetic, warm color grading, "
        "no text, no watermark, no logo",
        "Candid lifestyle detail shot: a desk with a notebook, a cup of tea and "
        "sunlight coming through a window, cozy home interior, realistic "
        "amateur photography look, natural shadows, no visible faces, no text, "
        "no watermark",
    ],
    "travel_landscape": [
        "Realistic landscape photograph of a calm lake surrounded by forested "
        "hills at golden hour, natural warm sunlight, soft clouds, DSLR photo "
        "quality, believable real-world location, no fantasy elements, no "
        "text, no watermark, no people",
        "Realistic mountain valley view at sunrise, layered mist between "
        "ridgelines, natural muted color palette, believable real travel "
        "photo aesthetic, shot on a wide-angle lens, no text overlay, no "
        "watermark, no people",
    ],
    "style_fashion": [
        "Editorial fashion photo, person from the neck down or from behind "
        "wearing a clean minimalist outfit (jacket, plain t-shirt, trousers), "
        "walking on an urban street, natural daylight, shallow depth of "
        "field, premium but realistic editorial photography look, correct "
        "human anatomy, no text, no watermark, no visible identifiable face",
        "Flat-lay style fashion photo of a neatly arranged outfit (jacket, "
        "shirt, watch, shoes) on a clean neutral background, soft studio "
        "lighting, realistic product/editorial photography, no text, no "
        "watermark",
    ],
    "motivation": [
        "Quiet realistic photo of an open notebook and a pen on a wooden desk "
        "next to a cup of coffee, morning window light, calm and minimal "
        "composition, realistic photography look, no text baked into the "
        "image, no watermark, no people",
    ],
}


def pick_theme_for_slot(recent_themes: list[str]) -> str:
    """Weighted random pick that avoids repeating the immediately preceding
    theme (the 'aynı içerik türünü arka arkaya paylaşma' rule)."""
    candidates = [t for t in THEMES if not recent_themes or t != recent_themes[-1]]
    weights = [THEME_WEIGHTS[t] for t in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def generate_caption(theme: str, used_recently: set[str]) -> str:
    pool = [c for c in CAPTION_BANK[theme] if c not in used_recently]
    if not pool:
        pool = CAPTION_BANK[theme]
    return random.choice(pool)


def generate_hashtags(theme: str, used_sets_recently: list[list[str]], n: int = 8) -> list[str]:
    pool = HASHTAG_POOL[theme][:]
    random.shuffle(pool)
    candidate = pool[:n]
    # avoid emitting a set nearly identical to the last one used for this theme
    for prev in used_sets_recently:
        overlap = len(set(candidate) & set(prev))
        if prev and overlap / max(len(prev), 1) > 0.7:
            random.shuffle(pool)
            candidate = pool[:n]
    return candidate


def generate_image_prompt(theme: str) -> str:
    return random.choice(IMAGE_PROMPT_TEMPLATES[theme])
