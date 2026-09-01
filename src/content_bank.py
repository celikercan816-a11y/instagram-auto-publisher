"""Local, offline content-idea bank: caption fragments, hashtag pools, AI
image-prompt building blocks, and a shot-composition/repetition system --
all rule-based rotation, no LLM API call involved (see the original 2026-08-31
rationale below, still true).

Rationale (documented since it's a real design choice): fully free-form,
always-fresh Turkish captions would need an LLM API call per post, which means
another paid key and another failure point in a headless GitHub Actions job.
Instead this bank holds enough hand-written variety per theme, and
content_quality.py rejects near-duplicates against content_history.json so the
same fragment/hashtag-set combination doesn't repeat too often. If richer
variation is wanted later, swap generate_caption()/generate_image_prompt() for
a real LLM call -- everything downstream (queue schema, quality gate, planner)
already treats caption/image_prompt as opaque strings.

Content strategy v2 (2026-09-01, user-directed): the account must read as a
real person's personal Instagram, not a single-theme "lifestyle influencer"
account. Two independent axes now drive every generated post:

  - THEME ("what is this post about"): 8 categories --
    sehir_istanbul / seyahat / gunluk_hayat / stil / spor_futbol /
    otomobil_yol / sosyal_yasam / detay_estetik. Supersedes the original 5
    (travel_landscape/style_fashion/lifestyle/automotive/creative_concept);
    THEME_ALIASES maps the old names so any already-planned/queued item from
    before this change still resolves correctly instead of KeyError-ing.

  - SHOT_TYPE ("how visible is the person"): face_visible /
    distant_or_profile_or_back / lifestyle_detail_no_face /
    location_landscape_no_person / style_accessory_detail /
    experimental_spontaneous, weighted toward a long-run ~30/20/20/15/10/5
    split (SHOT_TYPE_WEIGHTS) -- not every post needs to show the user's face.

Captions are no longer always a full sentence -- CAPTION_STYLES mixes no
caption, 1-3 words, an emoji, a "📍 Location" tag, a plain one-liner, and
(rarely) a more reflective sentence, matching how a real personal account
actually posts.

Repetition control (src/content_quality.check_attribute_repetition and
generate_content_attributes below) tracks the last 10 published posts'
theme/location/outfit/pose/camera_angle/time_of_day combination so e.g.
"Boğaz + siyah tişört + yan profil + gece" doesn't get regenerated right
after it was just used.

Not every image should look like a polished shoot -- IMPERFECTION_VARIANTS
occasionally (always, for experimental_spontaneous) adds a phone-snapshot/
motion-blur/off-center-framing note to the prompt, per explicit instruction
that "the photo doesn't have to be perfect to be good."
"""
import json
import random
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# ---------------------------------------------------------------------------
# Themes ("what is this post about")
# ---------------------------------------------------------------------------
THEMES = [
    "sehir_istanbul", "seyahat", "gunluk_hayat", "stil",
    "spor_futbol", "otomobil_yol", "sosyal_yasam", "detay_estetik",
]

# Old (pre-2026-09-01) theme names -> their closest new equivalent. Applied by
# resolve_theme() at the top of every theme-keyed lookup below, so an
# already-planned weekly_content_plan.json slot or an already-pending
# content_queue.json item created before this change keeps working instead of
# KeyError-ing on the old name.
THEME_ALIASES = {
    "travel_landscape": "seyahat",
    "style_fashion": "stil",
    "lifestyle": "gunluk_hayat",
    "automotive": "otomobil_yol",
    "creative_concept": "detay_estetik",
}

THEME_WEIGHTS = {
    "sehir_istanbul": 0.18,
    "seyahat": 0.18,
    "gunluk_hayat": 0.20,
    "stil": 0.14,
    "spor_futbol": 0.06,
    "otomobil_yol": 0.08,
    "sosyal_yasam": 0.10,
    "detay_estetik": 0.06,
}

STRATEGY_WEIGHTS_PATH = PROJECT_ROOT / "strategy_weights.json"
# How far a theme's weight is allowed to drift from its base value based on
# performance data -- keeps "boost what works" from turning into "only ever
# post one theme" (explicit user instruction).
MAX_PERFORMANCE_ADJUSTMENT = 0.20  # +/-20% relative to the theme's base weight
MIN_SAMPLES_TO_TRUST = 5


def resolve_theme(theme: str | None) -> str | None:
    """Maps a pre-2026-09-01 theme name to its new equivalent; passes new
    names (and None) through unchanged. Call this at the top of anything
    keyed by theme so old in-flight plan/queue items never KeyError."""
    return THEME_ALIASES.get(theme, theme)


# ---------------------------------------------------------------------------
# Shot types ("how visible is the person in this post")
# ---------------------------------------------------------------------------
SHOT_TYPES = [
    "face_visible", "distant_or_profile_or_back", "lifestyle_detail_no_face",
    "location_landscape_no_person", "style_accessory_detail", "experimental_spontaneous",
]

# User-facing reporting labels (2026-09-01), one per shot_type -- a naming
# layer over the same underlying mechanics, not a new generation pathway:
#   REAL_PERSON_COMPOSITE / PERSON_HIDDEN -> always src/person_composite.py
#     (a real reference_photos/ photo, never an AI-generated person)
#   OBJECT_LIFESTYLE / LANDSCAPE -> always the existing FLUX text-to-image
#     path, now with zero body-part tolerance (see SHOT_TYPE_FRAMING)
SHOT_TYPE_CATEGORY = {
    "face_visible": "REAL_PERSON_COMPOSITE",
    "experimental_spontaneous": "REAL_PERSON_COMPOSITE",
    "distant_or_profile_or_back": "PERSON_HIDDEN",
    "lifestyle_detail_no_face": "OBJECT_LIFESTYLE",
    "style_accessory_detail": "OBJECT_LIFESTYLE",
    "location_landscape_no_person": "LANDSCAPE",
}

# Long-run target distribution (not a per-week quota) per explicit instruction.
SHOT_TYPE_WEIGHTS = {
    "face_visible": 0.30,
    "distant_or_profile_or_back": 0.20,
    "lifestyle_detail_no_face": 0.20,
    "location_landscape_no_person": 0.15,
    "style_accessory_detail": 0.10,
    "experimental_spontaneous": 0.05,
}

SHOT_TYPE_FRAMING = {
    "face_visible": "the person's face is clearly visible, natural unposed expression, not staring into the camera",
    "distant_or_profile_or_back": "the person is shown from a distance, in profile, or from behind -- face not clearly visible",
    # "no face"/"no people" were strengthened 2026-09-01 after a text-to-image
    # generation for lifestyle_detail_no_face produced a full, generic
    # stranger's face reaching toward the camera despite the original softer
    # wording -- these are the two shot types where a person must never be
    # the recognizable main subject, so the instruction is now explicit and
    # repeated (head/face excluded twice, in different words) rather than
    # relying on the model to infer it from framing language alone.
    #
    # 2026-09-01 re-definition (OBJECT_LIFESTYLE category): a coverage test
    # showed even "hands visible, no face" still measures as ~60% "person"
    # by pixel area -- since there is no free local face-detector to tell
    # "acceptable hand closeup" from "unwanted face", the category was
    # redefined to ban EVERY body part, not just the face. This makes the
    # existing person-PIXEL-coverage hard-fail check
    # (content_quality.check_unexpected_person) actually usable here again.
    "lifestyle_detail_no_face": "a pure object/still-life detail shot -- no person, no hands, no arms, no body parts, no human silhouette visible anywhere in the frame",
    "location_landscape_no_person": "no people anywhere in the frame, absolutely no human figures, a pure empty location/landscape shot",
    "style_accessory_detail": "a pure close-up on an outfit or accessory item by itself (e.g. resting on a surface) -- no person, no hands, no arms, no body parts, no human silhouette visible anywhere in the frame",
    "experimental_spontaneous": "a spontaneous, off-guard, slightly imperfect snapshot",
}

# Which pose/camera-angle ids are plausible for each shot type, and whether an
# outfit description belongs in the prompt at all (no outfit for a pure
# no-person landscape).
SHOT_TYPE_CONSTRAINTS = {
    "face_visible": {
        "camera_angles": ["yandan_profil", "goz_hizasi_selfie", "yuksek_aci"],
        "poses": ["oturup_uzaga_bakma", "elinde_kahve", "dogal_gulumseme", "dayanma", "dogal_hareket"],
        "needs_outfit": True,
    },
    "distant_or_profile_or_back": {
        "camera_angles": ["yandan_profil", "arkadan", "uzak_genis"],
        "poses": ["yururken", "arkadan_dogal", "oturup_uzaga_bakma", "dayanma"],
        "needs_outfit": True,
    },
    "lifestyle_detail_no_face": {
        # No poses: OBJECT_LIFESTYLE (2026-09-01) bans every body part, so
        # there is no person-pose to describe -- see SHOT_TYPE_FRAMING.
        "camera_angles": ["yakin_detay"],
        "poses": [],
        "needs_outfit": False,
    },
    "location_landscape_no_person": {
        "camera_angles": ["uzak_genis", "yuksek_aci"],
        "poses": [],
        "needs_outfit": False,
    },
    "style_accessory_detail": {
        "camera_angles": ["yakin_detay"],
        "poses": [],
        "needs_outfit": False,
    },
    "experimental_spontaneous": {
        "camera_angles": ["arkadan", "uzak_genis", "goz_hizasi_selfie", "yandan_profil"],
        "poses": ["dogal_hareket", "yururken", "dogal_gulumseme"],
        "needs_outfit": True,
    },
}

# Only "face_visible" gets the profile's face/hair description folded into the
# prompt (src/profile_style.py) -- per explicit instruction to prioritize
# non-face-forward shots rather than fake a close facial likeness the current
# free text-to-image provider can't reliably produce (see
# profile_style.json > known_limitations).
SHOT_TYPES_WITH_FACE_DESCRIPTION = {"face_visible"}

# ---------------------------------------------------------------------------
# Caption styles ("how much/what kind of text, if any")
# ---------------------------------------------------------------------------
CAPTION_STYLES = ["none", "short_word", "emoji", "location_tag", "casual_sentence", "thoughtful_sentence"]
CAPTION_STYLE_WEIGHTS = {
    "none": 0.15,
    "short_word": 0.20,
    "emoji": 0.15,
    "location_tag": 0.15,
    "casual_sentence": 0.25,
    "thoughtful_sentence": 0.10,
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
    "hustle", "grind mode", "mindset is everything", "unutulmaz bir gün",
    "her an bir hediye", "kalbinin sesini dinle",
]

# Generic engagement-bait tags never used regardless of theme.
SPAM_HASHTAGS = {"#viral", "#fyp", "#explorepage", "#follow", "#followme", "#like4like", "#instagood"}

# ---------------------------------------------------------------------------
# Attribute pools (location / outfit / pose / camera angle / time of day) --
# these both drive prompt composition AND get recorded on the queue item /
# content_history.json entry for the repetition check in content_quality.py.
# ---------------------------------------------------------------------------
ATTRIBUTE_FIELDS = ["theme", "location", "outfit", "pose", "camera_angle", "time_of_day"]

LOCATIONS: dict[str, list[dict]] = {
    "sehir_istanbul": [
        {"id": "bogaz_kiyisi", "en": "the Bosphorus waterfront with the strait and hills across the water", "tr": "Boğaz"},
        {"id": "vapur", "en": "aboard a ferry crossing the Bosphorus, deck railing and water visible", "tr": "Vapur"},
        {"id": "rooftop", "en": "a rooftop terrace overlooking the Istanbul skyline", "tr": "Rooftop"},
        {"id": "eski_sokak", "en": "an old cobblestone Istanbul side street with historic buildings", "tr": "İstanbul sokakları"},
        {"id": "sahil_kafe", "en": "a small waterside cafe table by the Bosphorus", "tr": "Sahil kafe"},
        {"id": "gece_koprusu", "en": "a bridge over the Bosphorus lit up at night, city lights reflecting on the water", "tr": "Boğaz gece"},
    ],
    "seyahat": [
        {"id": "ege_koy", "en": "a small whitewashed Aegean coastal town with narrow streets", "tr": "Ege"},
        {"id": "akdeniz_sahil", "en": "a Mediterranean beach with turquoise water and a palm-lined promenade", "tr": "Akdeniz"},
        {"id": "antalya_gezinti", "en": "a seaside promenade with mountains in the background and palm trees", "tr": "Antalya"},
        {"id": "afyon_manzara", "en": "a hilltop overlook with a lake and green valley below", "tr": "Afyon"},
        {"id": "dag_yolu", "en": "a mountain hiking trail surrounded by pine trees", "tr": "Dağ"},
        {"id": "otel_teras", "en": "a hotel balcony or terrace overlooking the sea", "tr": "Otel"},
        {"id": "marina", "en": "a small marina with boats docked along a wooden pier", "tr": "Marina"},
    ],
    "gunluk_hayat": [
        {"id": "ev_balkon", "en": "a home balcony with plants, a relaxed everyday setting", "tr": "Ev"},
        {"id": "masa_calisma", "en": "a desk with a laptop, notebook and coffee, a casual workspace", "tr": "Çalışma anı"},
        {"id": "kahve_dukkani", "en": "a small neighborhood coffee shop counter", "tr": "Kahve"},
        {"id": "yuruyus_parki", "en": "a city park path during a casual walk", "tr": "Yürüyüş"},
        {"id": "alisveris_sokagi", "en": "a shopping street with storefronts, a casual daytime errand", "tr": "Alışveriş"},
        {"id": "ev_kedi", "en": "a living room sofa at home with a white cat nearby", "tr": "Ev, kedi"},
        {"id": "arkadas_ortami", "en": "a casual group hangout at a table, other people present but blurred or turned away with no identifiable faces", "tr": "Arkadaş ortamı"},
    ],
    "stil": [
        {"id": "sokak_kombin", "en": "standing on a clean city sidewalk, outfit-focused shot", "tr": "Sokak stili"},
        {"id": "vitrin_onu", "en": "in front of a minimal storefront window, outfit-focused", "tr": "Stil"},
        {"id": "merdiven", "en": "sitting casually on urban steps, outfit visible", "tr": "Kombin"},
        {"id": "ayna_detay", "en": "a mirror selfie in a minimal room, outfit-focused, phone partially covering the face", "tr": "Ayna karesi"},
    ],
    "spor_futbol": [
        {"id": "stadyum_tribun", "en": "in a football stadium stand among a crowd of fans in generic yellow-and-navy colors, no visible club crest, logo or sponsor branding anywhere, no readable text on banners or jerseys, an atmosphere shot rather than a specific identifiable match", "tr": "Tribün"},
        {"id": "sehir_mac_gunu", "en": "walking through a city street on a match day wearing a plain generic yellow-and-navy scarf, no visible club logo or crest, no readable text", "tr": "Maç günü"},
        {"id": "stadyum_disi", "en": "outside a generic stadium exterior in the evening with floodlights visible, no readable signage or logos", "tr": "Stadyum"},
    ],
    "otomobil_yol": [
        {"id": "arac_ici_yol", "en": "inside a car during a daytime road trip, dashboard and windshield view", "tr": "Yolda"},
        {"id": "park_arac_yani", "en": "standing next to a parked car on a quiet street", "tr": "Araç"},
        {"id": "gece_yolculuk", "en": "driving at night, dashboard lights and the road ahead through the windshield", "tr": "Gece yolu"},
        {"id": "benzinlik_mola", "en": "a gas station rest stop at dusk, car partially visible", "tr": "Yol molası"},
    ],
    "sosyal_yasam": [
        {"id": "restoran_aksam", "en": "a restaurant table in the evening with warm indoor lighting", "tr": "Restoran"},
        {"id": "kahvalti_masasi", "en": "a breakfast table outdoors in the morning with plates and tea glasses", "tr": "Kahvaltı"},
        {"id": "arkadas_masasi", "en": "a table with friends in the background, out of focus, no identifiable faces of others, casual evening atmosphere", "tr": "Akşam"},
        {"id": "sehir_gecesi", "en": "a lively city street at night with lit shopfronts and restaurants", "tr": "Şehir gecesi"},
    ],
    "detay_estetik": [
        {"id": "saat_detay", "en": "a close-up of a wristwatch resting on a table next to a coffee cup", "tr": "Saat"},
        {"id": "ayakkabi_detay", "en": "a close-up of shoes on pavement or sand", "tr": "Ayakkabı"},
        {"id": "masa_detay", "en": "a flat-lay of a coffee cup, phone and sunglasses on a wooden table", "tr": "Masa"},
        {"id": "mimari_detay", "en": "a low-angle architectural detail shot of a building facade", "tr": "Mimari"},
        {"id": "manzara_detay", "en": "a wide scenic view of the sea or mountains, no people", "tr": "Manzara"},
        {"id": "yemek_detay", "en": "a close-up of a plated meal on a restaurant table", "tr": "Yemek"},
    ],
}

# Global (theme-independent) -- matches the profile's observed wardrobe.
OUTFITS = [
    {"id": "siyah_basic", "en": "a plain black t-shirt", "tr": "siyah basic"},
    {"id": "beyaz_ekru", "en": "a white or ecru linen short-sleeve shirt", "tr": "beyaz/ekru"},
    {"id": "haki_overshirt", "en": "an olive green overshirt over a plain t-shirt", "tr": "haki overshirt"},
    {"id": "polo", "en": "a simple polo shirt", "tr": "polo"},
    {"id": "smart_casual", "en": "a smart-casual button-up shirt with dark trousers", "tr": "smart casual"},
    {"id": "keten_gomlek", "en": "a linen shirt with sleeves rolled up", "tr": "keten gömlek"},
]

POSES = {
    "oturup_uzaga_bakma": "sitting, looking off into the distance, not at the camera",
    "yururken": "walking naturally, mid-stride",
    "elinde_kahve": "holding a coffee cup casually",
    "dogal_gulumseme": "a relaxed natural smile, slightly turned away from the camera",
    "arkadan_dogal": "seen from behind in a natural relaxed posture",
    "dogal_hareket": "caught mid-motion, natural and unposed",
    "dayanma": "leaning casually against a railing or wall",
}

CAMERA_ANGLES = {
    "yandan_profil": "side profile shot",
    "arkadan": "shot from behind",
    "uzak_genis": "a wide shot from a distance",
    "yakin_detay": "close-up detail framing",
    "goz_hizasi_selfie": "eye-level selfie-style framing",
    "yuksek_aci": "shot from a slightly elevated angle",
}

TIME_OF_DAY = {
    "gunduz_oglen": "bright midday daylight, not golden hour",
    "aksam_gun_batimi": "warm golden hour sunset light",
    "gece": "nighttime with artificial lighting",
    "alacakaranlik": "blue hour twilight just after sunset",
    "kapali_bulutlu": "soft overcast daylight",
    "sabah_erken": "soft early morning light",
}

# Sometimes (always for experimental_spontaneous) folded into the prompt so
# not every image reads as a polished professional shoot.
IMPERFECTION_VARIANTS = [
    "slight handheld motion blur, imperfect casual framing",
    "ordinary phone-snapshot quality, not professionally composed",
    "slightly grainy low-light phone photo, minor noise",
    "candid off-guard moment, slightly awkward crop, unpolished feel",
    "looks taken quickly by a friend, subject slightly off-center",
    "plain ordinary daylight snapshot, nothing dramatic or styled",
]
IMPERFECTION_PROBABILITY = 0.45

HASHTAG_POOL = {
    "sehir_istanbul": ["#istanbul", "#boğaz", "#şehir", "#gececekimi", "#vapur", "#sokak", "#rooftop", "#kafeler", "#istanbulhayatı", "#şehirestetiği"],
    "seyahat": ["#seyahat", "#gezi", "#türkiye", "#doğa", "#tatil", "#yolculuk", "#keşfet", "#akdeniz", "#ege", "#manzara"],
    "gunluk_hayat": ["#günlükhayat", "#sadelik", "#anlar", "#gündelik", "#huzur", "#kahvekeyfi", "#yavaşyaşam", "#bugün", "#evhali", "#kedi"],
    "stil": ["#stil", "#kombin", "#günlükstil", "#erkekstili", "#sonbaharstili", "#basitşıklık", "#saat", "#aksesuar", "#smartcasual"],
    "spor_futbol": ["#maçgünü", "#tribün", "#futbol", "#stadyum", "#atmosfer", "#taraftar"],
    "otomobil_yol": ["#otomobil", "#yolculuk", "#roadtrip", "#arabatutkusu", "#yolda", "#direksiyon"],
    "sosyal_yasam": ["#akşamyemeği", "#restoran", "#kahvaltı", "#sosyalhayat", "#şehirgecesi", "#keyifanı"],
    "detay_estetik": ["#detay", "#kompozisyon", "#estetik", "#minimal", "#kareyakalamak", "#mimarî"],
}

# ---------------------------------------------------------------------------
# Caption text pools
# ---------------------------------------------------------------------------
SHORT_WORD_BANK = [
    "İyi geldi.", "Bugünden.", "Akşam turu.", "Biraz deniz.", "Hafta sonu.",
    "Yoldayız.", "Sakin bir gün.", "Kısa bir mola.", "Bu da bugünden.", "Yine buradayız.",
    "İstanbul.", "Akşam.",
]

EMOJI_BANK = {
    "sehir_istanbul": ["🌉", "🏙️", "⛴️"],
    "seyahat": ["🌊", "🏝️", "🗺️"],
    "gunluk_hayat": ["☕", "🐾", "🏠"],
    "stil": ["🖤", "👞"],
    "spor_futbol": ["⚽", "🎽"],
    "otomobil_yol": ["🚗", "🛣️"],
    "sosyal_yasam": ["🍽️", "🥂"],
    "detay_estetik": ["📸", "⌚"],
}
GENERIC_EMOJI = ["📍", "✨"]

CASUAL_SENTENCE_BANK = {
    "sehir_istanbul": ["Bugün Boğaz tarafındaydık.", "Kısa bir şehir turu.", "Vapur her zaman iyi geliyor.", "Bugün İstanbul biraz farklıydı."],
    "seyahat": ["Birkaç günlüğüne buradaydık.", "Yol uzundu, manzara buna değdi.", "Bu sefer rota biraz farklıydı.", "Kısa bir kaçamak oldu."],
    "gunluk_hayat": ["Bugün böyle geçti.", "Kahve iyi geldi.", "Ufak bir mola.", "Bugün plan yoktu, iyi de oldu.", "Ev hali."],
    "stil": ["Bugünkü kombin.", "Basit ama oldu.", "Dolaptan çıkan.", "Bugün böyle giyindim."],
    "spor_futbol": ["Tribün enerjisi.", "Maç günü.", "Bugün stadyumdaydık.", "Atmosfer güzeldi."],
    "otomobil_yol": ["Yol uzundu ama iyiydi.", "Biraz sürüş iyi geldi.", "Direksiyon başında bir mola.", "Yoldayız."],
    "sosyal_yasam": ["Akşam güzeldi.", "Masa keyifliydi.", "Bugün dışarıdaydık.", "Kısa bir akşam yemeği."],
    "detay_estetik": ["Bugünden bir kare.", "Küçük bir detay.", "Bu kareyi sevdim.", "Basit ama sevdim."],
}

# Used rarely (CAPTION_STYLE_WEIGHTS["thoughtful_sentence"] = 0.10) -- never
# asserts a specific real place/date/event/person to avoid the "gerçek hayat
# iddiası" risk flagged in content_quality.check_caption's REAL_CLUB_NAME
# guard and this module's own docstring.
THOUGHTFUL_SENTENCE_BANK = {
    "sehir_istanbul": ["Bu şehri kaç kere görsem yine şaşırıyorum.", "Bazı akşamlar İstanbul'u izlemek yeterli oluyor."],
    "seyahat": ["Bazı yerler fotoğrafta bile eksik kalıyor, burası öyle.", "Haritada küçük bir nokta, gerçekte hiç öyle değil."],
    "gunluk_hayat": ["Bazı günler planlı değil, öyle daha iyi.", "Basit bir gün, basit bir kare."],
    "stil": ["Bazen az şey, çok şey ifade ediyor.", "Bu renk uzun zamandır dolapta bekliyordu."],
    "spor_futbol": ["Tribünün sesi bambaşka bir şey.", "Bu atmosferi kaçırmak istemedim."],
    "otomobil_yol": ["Bazı yolculuklar varış noktasından çok yolun kendisiyle ilgili.", "Uzun yol iyi gelir bazen, düşünmeden sürmek."],
    "sosyal_yasam": ["Bu mekana bir işim için uğramıştım, kalmak istedim.", "Bazen en iyi sohbetler plansız oluyor."],
    "detay_estetik": ["Bazen sahne kurmak, anı yakalamaktan daha eğlenceli.", "Basit şeyleri yan yana koyunca bazen iyi bir kare çıkıyor."],
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


def pick_shot_type_for_slot(recent_shot_types: list[str]) -> str:
    """Same idea as pick_theme_for_slot but for the face-visibility axis."""
    candidates = [s for s in SHOT_TYPES if not recent_shot_types or s != recent_shot_types[-1]]
    weights = [SHOT_TYPE_WEIGHTS[s] for s in candidates]
    return random.choices(candidates, weights=weights, k=1)[0]


def _location_lookup(theme: str, location_id: str | None) -> dict:
    pool = LOCATIONS[theme]
    for loc in pool:
        if loc["id"] == location_id:
            return loc
    return random.choice(pool)


def _outfit_lookup(outfit_id: str | None) -> dict:
    for outfit in OUTFITS:
        if outfit["id"] == outfit_id:
            return outfit
    return random.choice(OUTFITS)


def _max_overlap(candidate: dict, recent_entries: list[dict]) -> int:
    """Highest number of ATTRIBUTE_FIELDS that match between candidate and
    any single one of recent_entries' stored 'attributes' dict."""
    best = 0
    for entry in recent_entries:
        other = entry.get("attributes") or {}
        overlap = sum(
            1 for f in ATTRIBUTE_FIELDS
            if candidate.get(f) is not None and candidate.get(f) == other.get(f)
        )
        best = max(best, overlap)
    return best


def generate_content_attributes(theme: str, shot_type: str, history: list[dict], max_attempts: int = 8) -> dict:
    """Picks location/outfit/pose/camera_angle/time_of_day for this post,
    resampling (up to max_attempts times) to avoid closely matching the
    combination used in any of the last 10 published posts -- e.g. avoids
    regenerating 'Boğaz + siyah tişört + yan profil + gece' right after it
    was just used. Never raises/loops forever: falls back to the
    least-overlapping candidate found if a perfectly fresh one isn't."""
    from src.content_history import last_n

    theme = resolve_theme(theme)
    constraints = SHOT_TYPE_CONSTRAINTS[shot_type]
    recent10 = last_n(history, 10)

    best_candidate = None
    best_overlap = None
    for _ in range(max_attempts):
        location = random.choice(LOCATIONS[theme])["id"]
        outfit = random.choice(OUTFITS)["id"] if constraints["needs_outfit"] else None
        pose = random.choice(constraints["poses"]) if constraints["poses"] else None
        camera_angle = random.choice(constraints["camera_angles"])
        time_of_day = random.choice(list(TIME_OF_DAY.keys()))
        candidate = {
            "theme": theme, "shot_type": shot_type, "location": location, "outfit": outfit,
            "pose": pose, "camera_angle": camera_angle, "time_of_day": time_of_day,
        }
        overlap = _max_overlap(candidate, recent10)
        if overlap == 0:
            return candidate
        if best_overlap is None or overlap < best_overlap:
            best_overlap, best_candidate = overlap, candidate
    return best_candidate


def compose_caption(caption_text: str, hashtags: list[str]) -> str:
    hashtag_str = " ".join(hashtags)
    if not caption_text:
        return hashtag_str
    return caption_text + "\n\n" + hashtag_str



# Styles that describe or imply a specific real place/moment actually lived
# through. Fine for media_source == "local" (a real photo -- the claim is
# true), but never used for AI-generated media (2026-09-01 fix): an AI scene
# must not carry a caption implying the user was really there, really did
# something, or is really at a specific pinned location, since none of that
# is true. NON_CLAIM_CAPTION_STYLES are the only ones considered safe with no
# certainty about whether the moment is real.
CLAIM_IMPLYING_CAPTION_STYLES = {"location_tag", "casual_sentence", "thoughtful_sentence"}
NON_CLAIM_CAPTION_STYLES = [s for s in CAPTION_STYLES if s not in CLAIM_IMPLYING_CAPTION_STYLES]


def generate_caption(
    theme: str,
    used_recently: set[str],
    history: list[dict] | None = None,
    media_source: str | None = None,
) -> tuple[str, str]:
    """Returns (caption_text, caption_style). caption_text can legitimately be
    '' (style 'none') -- compose_caption() handles that without a stray blank
    line. caption_style is picked avoiding an immediate repeat of the last
    published post's style (mirrors pick_theme_for_slot's approach).

    media_source should be "local" (a real, actually-taken photo) or
    "ai_generated"/None -- for anything other than a real local photo, the
    style pool is restricted to NON_CLAIM_CAPTION_STYLES (none/short_word/
    emoji) so an AI scene never gets a caption asserting a specific real
    place, event or lived moment that didn't happen."""
    from src.content_history import last_n

    theme = resolve_theme(theme)
    history = history or []
    allowed_styles = CAPTION_STYLES if media_source == "local" else NON_CLAIM_CAPTION_STYLES
    recent_styles = [
        (e.get("attributes") or {}).get("caption_style")
        for e in last_n(history, 5)
        if (e.get("attributes") or {}).get("caption_style")
    ]
    styles = [s for s in allowed_styles if not recent_styles or s != recent_styles[-1]]
    weights = [CAPTION_STYLE_WEIGHTS[s] for s in styles]
    style = random.choices(styles, weights=weights, k=1)[0]

    if style == "none":
        text = ""
    elif style == "short_word":
        text = random.choice(SHORT_WORD_BANK)
    elif style == "emoji":
        text = random.choice(EMOJI_BANK.get(theme, GENERIC_EMOJI))
    elif style == "location_tag":
        loc = random.choice(LOCATIONS[theme])
        text = f"📍 {loc['tr']}"
    elif style == "casual_sentence":
        pool = [c for c in CASUAL_SENTENCE_BANK[theme] if c not in used_recently]
        text = random.choice(pool or CASUAL_SENTENCE_BANK[theme])
    else:  # thoughtful_sentence
        pool = [c for c in THOUGHTFUL_SENTENCE_BANK[theme] if c not in used_recently]
        text = random.choice(pool or THOUGHTFUL_SENTENCE_BANK[theme])

    return text, style


def generate_hashtags(theme: str, used_sets_recently: list[list[str]], n: int | None = None) -> list[str]:
    """n defaults to a random 4-8 (per-post variety, per instruction), never
    includes SPAM_HASHTAGS, and avoids near-repeating the last set used for
    this theme."""
    theme = resolve_theme(theme)
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


def generate_image_prompt(
    theme: str,
    shot_type: str | None = None,
    attributes: dict | None = None,
    profile: dict | None = None,
) -> str:
    """Composes a prompt from theme (setting) x shot_type (how the person
    appears, if at all) x attributes (specific location/outfit/pose/camera/
    time-of-day) x profile_style.json directives (only for shot_type ==
    'face_visible' -- see SHOT_TYPES_WITH_FACE_DESCRIPTION) x an occasional
    imperfection note x the shared realism suffix.

    Every argument beyond theme is optional and auto-picked, so existing
    callers that only pass theme keep working exactly as before."""
    theme = resolve_theme(theme)
    shot_type = shot_type or pick_shot_type_for_slot([])
    attrs = attributes or generate_content_attributes(theme, shot_type, [])

    location = _location_lookup(theme, attrs.get("location"))
    framing = SHOT_TYPE_FRAMING[shot_type]
    time_desc = TIME_OF_DAY[attrs.get("time_of_day") or random.choice(list(TIME_OF_DAY.keys()))]
    camera_desc = CAMERA_ANGLES[attrs.get("camera_angle") or random.choice(SHOT_TYPE_CONSTRAINTS[shot_type]["camera_angles"])]

    # OBJECT_LIFESTYLE (lifestyle_detail_no_face/style_accessory_detail) and
    # LANDSCAPE (location_landscape_no_person) ban every body part -- see
    # SHOT_TYPE_FRAMING's 2026-09-01 note -- so none of them get a subject
    # clause; only face_visible/distant_or_profile_or_back/
    # experimental_spontaneous describe a person at all.
    NO_PERSON_SHOT_TYPES = {"location_landscape_no_person", "lifestyle_detail_no_face", "style_accessory_detail"}
    person_present = shot_type not in NO_PERSON_SHOT_TYPES
    if person_present:
        outfit = _outfit_lookup(attrs.get("outfit"))
        pose_id = attrs.get("pose")
        pose_desc = POSES.get(pose_id, "") if pose_id else ""
        subject = f"a man wearing {outfit['en']}" + (f", {pose_desc}" if pose_desc else "") + ", "
    else:
        subject = "no people, "

    prompt = (
        f"Realistic candid iPhone-style photo, {subject}{location['en']}, "
        f"{framing}, {camera_desc}, {time_desc}"
    )

    if shot_type == "experimental_spontaneous" or random.random() < IMPERFECTION_PROBABILITY:
        prompt += ", " + random.choice(IMPERFECTION_VARIANTS)

    prompt += _REALISM_SUFFIX

    if person_present:
        from src.profile_style import build_prompt_style_directives, load_profile_style
        include_face = shot_type in SHOT_TYPES_WITH_FACE_DESCRIPTION
        active_profile = profile if profile is not None else load_profile_style()
        prompt += build_prompt_style_directives(theme, active_profile, include_face=include_face)

    return prompt
