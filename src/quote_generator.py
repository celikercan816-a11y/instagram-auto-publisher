"""Turkish quote/short-poem/proverb generation for the "manzara + söz"
content pivot (2026-09-01) -- this replaces PERSON_VISIBLE/PERSON_HIDDEN as
the account's main content type. Fully offline, rule-based rotation (same
philosophy as content_bank.py's original design rationale): a hand-curated
bank per category, picked with repetition-avoidance against
content_history.json, not a per-post LLM call.

Categories (QUOTE_CATEGORY_WEIGHTS below implements the ~45/20/20/15 split):
  ORIGINAL          -- short original Turkish sentences, aphoristic tone.
                       Genuinely written for this project, never a
                       reproduction of any of the user's own example lines.
  SHORT_POEM        -- 2-4 line original short poems, same authorship note.
  PROVERB           -- ONLY entries independently confirmed to be genuine,
                       standard Turkish atasözü (see PROVERBS' 2026-09-01
                       review note). No named author, no attribution risk.
  COMMON_SAYING     -- a widely-circulated saying that READS like a proverb
                       but isn't confidently verified as a classical/
                       documented atasözü (point 2's fix: "emin
                       olunamıyorsa PROVERB olarak sınıflandırma" --
                       "Zaman her şeyin ilacıdır." moved here from PROVERB
                       for exactly this reason). Still zero attribution risk.
  ANONYMOUS         -- short traditional/folk wisdom lines, always labeled
                       "Anonim" or shown with no author at all.
  PUBLIC_DOMAIN     -- named-author quotes. Deliberately seeded with only a
                       handful of unambiguous, extremely well-documented
                       public-domain lines (Atatürk, İstiklal Marşı) --
                       NONE of the figures the user explicitly flagged as
                       high misattribution-risk (Mevlana, Yunus Emre,
                       Nietzsche, Kafka, Bukowski, Cemal Süreya, Can Yücel)
                       are included, because this module has no reliable way
                       to verify exact wording/attribution for those, and
                       the user was explicit that an unverifiable quote must
                       never carry a named author. Grow this list only with
                       quotes that have been independently verified.

Each quote is tagged with a `mood` used to pick a thematically-matching
landscape (see LANDSCAPE_SCENES and select_scene_for_mood()) -- point 8 of
the approved design: "içeriği rastgele görselle eşleştirme."
"""
import hashlib
import random
import re
import unicodedata

from src.content_history import last_n

# Locked 2026-09-01 after 4 test rounds (41 -> 142 -> 400 -> 100 candidates)
# converged on a calibration the user approved: quote_impact.py's 85-point
# floor, QUALITY_BANDS, and the family-aware (A/B/C) editorial judgment
# described there. Bump this only for an intentional, reviewed change to
# how quotes are scored/selected -- not for routine bank additions.
ORIGINAL_QUOTE_ENGINE_VERSION = "1.0"

MOODS = ["yalnizlik", "umut", "zaman", "yol", "huzur", "melankoli"]

QUOTE_CATEGORY_WEIGHTS = {
    "ORIGINAL": 0.45,
    "SHORT_POEM": 0.20,
    "PROVERB": 0.20,
    "PUBLIC_DOMAIN": 0.15,
}
# ANONYMOUS and COMMON_SAYING are folded into PROVERB's slot at generation
# time (all three are "no personal attribution risk" categories, see
# generate_quote()) rather than given their own weight slice -- the 20%
# budget is split three ways each time a pick is made from that slot.

# ---------------------------------------------------------------------------
# A) ORIGINAL -- genuinely written for this project, aphoristic/short tone.
# ---------------------------------------------------------------------------
ORIGINAL_QUOTES = [
    {"text": "Bazı sokaklar seni eve değil,\nkendine götürür.", "mood": "yol"},
    {"text": "Uzaklaşmak bazen kaybolmak değil,\nkendini bulmaktır.", "mood": "yol"},
    {"text": "Deniz hep aynı yerde durur,\nsen her seferinde başka bakarsın.", "mood": "huzur"},
    {"text": "Sessizlik bazen boşluk değil,\ndinlenmiş bir zihindir.", "mood": "huzur"},
    {"text": "Zaman geçmez,\nsadece sen biraz daha hafiflersin.", "mood": "zaman"},
    {"text": "Her şehir birini bekletir,\nama kimseyi sonsuza dek tutmaz.", "mood": "melankoli"},
    {"text": "Işıklar söndüğünde de\nşehir hâlâ oradadır.", "mood": "yalnizlik"},
    {"text": "Bazı yolculuklar dönüş için değil,\nvazgeçmemek için çıkılır.", "mood": "umut"},
    {"text": "Yağmur bir şeyi bitirmez,\nsadece havayı temizler.", "mood": "umut"},
    {"text": "Kimse aynı nehre iki kez girmez,\nşehir de her seferinde biraz başkadır.", "mood": "zaman"},
    {"text": "Yalnızlık her zaman boşluk değildir,\nbazen sadece sessiz bir oda.", "mood": "yalnizlik"},
    {"text": "Uzak bir ışık bile\nyolun var olduğunu hatırlatır.", "mood": "umut"},
    {"text": "Bazı vedalar bitiş değildir,\nsadece mesafe alır.", "mood": "melankoli"},
    {"text": "Sisin içinde yürümek,\nyine de bir yöndür.", "mood": "umut"},
    {"text": "Şehir gürültüsü bazen\nen sessiz düşünceleri saklar.", "mood": "yalnizlik"},
    {"text": "Bir yeri özlemek,\norayı hâlâ taşıdığın anlamına gelir.", "mood": "melankoli"},
    {"text": "Dağ kımıldamaz,\nama sen ona her defasında farklı çıkarsın.", "mood": "huzur"},
    {"text": "Bazı sabahlar hiçbir şey vaat etmez,\nsadece yeniden başlatır.", "mood": "umut"},
]

# ---------------------------------------------------------------------------
# B) SHORT_POEM -- 2-4 lines, original, no forced rhyme.
# ---------------------------------------------------------------------------
SHORT_POEMS = [
    {"text": "Rıhtımda bekleyen tek kişi bendim,\nvapur yine de tam vaktinde geldi.", "mood": "yalnizlik"},
    {"text": "Sokak ıslak, ışıklar uzun,\nkimse yok ama şehir hâlâ konuşuyor.", "mood": "melankoli"},
    {"text": "Dağın tepesinde rüzgâr başka,\nne bıraktığını orada anlıyorsun.", "mood": "huzur"},
    {"text": "Sis kalkınca deniz aynı,\nama sen ona az önce baktığın gibi bakmıyorsun.", "mood": "zaman"},
    {"text": "Gece şehri unutmaz,\nsadece ışıklarını azaltır.", "mood": "yalnizlik"},
    {"text": "Yol uzadıkça konuşma azaldı,\nsonunda sadece manzara kaldı.", "mood": "yol"},
    {"text": "Kahve soğudu, pencere ıslak,\nbir yere gitmeye gerek yoktu bugün.", "mood": "huzur"},
    {"text": "Kar şehri değiştirmiyor,\nsadece bir süreliğine sessize alıyor.", "mood": "huzur"},
    {"text": "Eski sokakta hâlâ bir ışık yanıyor,\nkimin için, artık kimse bilmiyor.", "mood": "zaman"},
    {"text": "Gün doğarken kimse alkışlamıyor,\nyine de her şey yeniden başlıyor.", "mood": "umut"},
]

# ---------------------------------------------------------------------------
# C) PROVERB -- ONLY entries independently confirmed (2026-09-01 review) to
# be genuine, standard Turkish atasözü as documented in classical atasözü
# compilations (e.g. Ömer Asım Aksoy) -- no named author, no attribution
# risk. Point 2's explicit fix: "Zaman her şeyin ilacıdır." (a translated
# aphorism, not a documented atasözü), "Bugünün işini yarına bırakma." and
# "Deniz dalgasız olmaz." (plausible-sounding but not confidently verified
# as classical atasözü) were REMOVED from here and moved to COMMON_SAYING
# below -- "emin olunamıyorsa PROVERB olarak sınıflandırma" rule.
# ---------------------------------------------------------------------------
PROVERBS = [
    {"text": "Damlaya damlaya göl olur.", "mood": "umut"},
    {"text": "Ağaç yaşken eğilir.", "mood": "zaman"},
    {"text": "Sabreden derviş muradına ermiş.", "mood": "umut"},
    {"text": "Gülü seven dikenine katlanır.", "mood": "melankoli"},
    {"text": "Yalnız taş duvar olmaz.", "mood": "yalnizlik"},
    {"text": "Rüzgâr eken fırtına biçer.", "mood": "yol"},
    {"text": "Akan su yolunu bulur.", "mood": "yol"},
    {"text": "Dost kara günde belli olur.", "mood": "yalnizlik"},
    {"text": "Sona kalan dona kalır.", "mood": "zaman"},
]

# ---------------------------------------------------------------------------
# C2) COMMON_SAYING -- widely circulated Turkish sayings that READ like
# proverbs but are NOT confidently verified as classical/documented
# atasözü (point 2's new category for exactly this uncertainty). Still zero
# attribution risk (no name attached, never will be), just a more honest
# label than PROVERB for a saying whose folk-proverb pedigree isn't certain.
# ---------------------------------------------------------------------------
COMMON_SAYINGS = [
    {"text": "Zaman her şeyin ilacıdır.", "mood": "zaman"},
    {"text": "Bugünün işini yarına bırakma.", "mood": "zaman"},
    {"text": "Deniz dalgasız olmaz.", "mood": "huzur"},
    {"text": "Her şeyin bir zamanı vardır.", "mood": "zaman"},
    {"text": "En karanlık an, şafaktan öncesidir.", "mood": "umut"},
]

# ---------------------------------------------------------------------------
# D) ANONYMOUS -- traditional/folk wisdom, always shown as "Anonim" or with
# no author line at all (never a real name).
# ---------------------------------------------------------------------------
ANONYMOUS_QUOTES = [
    {"text": "Her gidiş bir dönüşü içinde taşır.", "mood": "yol"},
    {"text": "Deniz kenarında dert küçülür derler.", "mood": "huzur"},
    {"text": "Sabah ezanı en sessiz vaattir.", "mood": "umut"},
    {"text": "Uzak yolun sonu, insanın kendisidir.", "mood": "yol"},
    {"text": "Şehir unutmaz, sadece biriktirir.", "mood": "melankoli"},
    {"text": "Yolun uzunu, insanı olgunlaştırır.", "mood": "yol"},
]

# ---------------------------------------------------------------------------
# E) PUBLIC_DOMAIN -- deliberately small, deliberately conservative. Only
# unambiguous, extremely well-documented public-domain Turkish lines --
# NONE of the high-misattribution-risk figures the user named.
# ---------------------------------------------------------------------------
PUBLIC_DOMAIN_QUOTES = [
    {"text": "Muhtaç olduğun kudret,\ndamarlarındaki asil kanda mevcuttur.", "author": "Mustafa Kemal Atatürk", "mood": "umut"},
    {"text": "Korkma, sönmez bu şafaklarda yüzen al sancak.", "author": "Mehmet Âkif Ersoy", "mood": "umut"},
    {"text": "Sanatsız kalan bir milletin\nhayat damarlarından biri kopmuştur.", "author": "Mustafa Kemal Atatürk", "mood": "zaman"},
]

REAL_CLICHE_MARKERS = [
    "hayallerinin peşinden git", "hayatını yaşa", "asla pes etme",
    "pozitif enerji", "kendine inan", "dream big", "good vibes only",
]


def _normalize_for_hash(text: str) -> str:
    """Lowercases, strips accents-insensitive punctuation/whitespace
    differences so near-identical re-picks of the same quote (extra
    whitespace, punctuation) still hash the same for repetition checks."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def quote_hash(text: str) -> str:
    return hashlib.sha256(_normalize_for_hash(text).encode("utf-8")).hexdigest()[:16]


def _pick_from(pool: list[dict], used_hashes: set[str]) -> dict:
    candidates = [q for q in pool if quote_hash(q["text"]) not in used_hashes]
    return dict(random.choice(candidates or pool))


def generate_quote(used_hashes: set[str] | None = None) -> dict:
    """Returns {"text", "author" (or None), "category", "mood", "hash"}.
    used_hashes should be the set of quote_hash() values from
    content_history.json's last ~50 entries (see check_quote_repetition in
    quote_quality.py) -- picks avoid repeating any of them when possible."""
    used_hashes = used_hashes or set()
    categories = list(QUOTE_CATEGORY_WEIGHTS.keys())
    weights = list(QUOTE_CATEGORY_WEIGHTS.values())
    category = random.choices(categories, weights=weights, k=1)[0]

    if category == "ORIGINAL":
        q = _pick_from(ORIGINAL_QUOTES, used_hashes)
        author = None
    elif category == "SHORT_POEM":
        q = _pick_from(SHORT_POEMS, used_hashes)
        author = None
    elif category == "PROVERB":
        # PROVERB slot is shared with COMMON_SAYING and ANONYMOUS (all three
        # are "no personal attribution" categories) -- 45/30/25 split.
        roll = random.random()
        if roll < 0.45:
            q = _pick_from(PROVERBS, used_hashes)
        elif roll < 0.75:
            q = _pick_from(COMMON_SAYINGS, used_hashes)
            category = "COMMON_SAYING"
        else:
            q = _pick_from(ANONYMOUS_QUOTES, used_hashes)
            category = "ANONYMOUS"
        author = None
    else:  # PUBLIC_DOMAIN
        q = _pick_from(PUBLIC_DOMAIN_QUOTES, used_hashes)
        author = q.get("author")

    return {
        "text": q["text"],
        "author": author,
        "category": category,
        "mood": q["mood"],
        "hash": quote_hash(q["text"]),
    }


# ---------------------------------------------------------------------------
# Caption (point 14): NEVER repeats the on-image quote text. Mostly a single
# emoji, a short theme word, or one short complementary sentence -- often
# empty. Never the same caption/hashtag set twice in a row.
# ---------------------------------------------------------------------------
CAPTION_STYLES = ["none", "emoji", "short_word", "short_sentence"]
CAPTION_STYLE_WEIGHTS = {"none": 0.30, "emoji": 0.25, "short_word": 0.20, "short_sentence": 0.25}

EMOJI_BY_MOOD = {
    "yalnizlik": ["🌙", "🖤"], "umut": ["🌅", "✨"], "zaman": ["⏳", "🍂"],
    "yol": ["🌊", "🛤️"], "huzur": ["🌊", "🍃"], "melankoli": ["🌫️", "🌙"],
}
SHORT_WORD_BY_MOOD = {
    "yalnizlik": ["Sessizlik.", "Gece."], "umut": ["Yeni bir gün.", "Umut."],
    "zaman": ["Zaman.", "Eski bir gün."], "yol": ["Yoldayız.", "Yolun sesi."],
    "huzur": ["Biraz sessizlik.", "Huzur."], "melankoli": ["Akşamdan kalan.", "Biraz hüzün."],
}
SHORT_SENTENCE_BY_MOOD = {
    "yalnizlik": ["Bugün biraz sessizlik iyi geldi.", "Bazen tek başına olmak yeterli."],
    "umut": ["Yeni bir sayfa gibi.", "Bugünden umutluyum."],
    "zaman": ["Bazı yerler zamanı unutturuyor.", "Burada saat başka işliyor."],
    "yol": ["Bugün yol biraz uzundu.", "Bazen sadece yürümek gerekiyor."],
    "huzur": ["Bugün her şey biraz yavaşladı.", "Bu manzara yeterliydi."],
    "melankoli": ["Bazı akşamlar böyle geçiyor.", "Hafif bir hüzün, kötü değil."],
}

HASHTAG_POOL = [
    "#sözler", "#şiir", "#istanbul", "#manzara", "#gece", "#huzur",
    "#sessizlik", "#anlar", "#şehir", "#deniz", "#doğa", "#yalnızlık",
    "#zaman", "#yol", "#akşam", "#atasözü", "#düşünce", "#sakinlik",
]


def generate_quote_caption(mood: str, recent_styles: list[str] | None = None) -> tuple[str, str]:
    recent_styles = recent_styles or []
    styles = [s for s in CAPTION_STYLES if not recent_styles or s != recent_styles[-1]]
    weights = [CAPTION_STYLE_WEIGHTS[s] for s in styles]
    style = random.choices(styles, weights=weights, k=1)[0]
    if style == "none":
        text = ""
    elif style == "emoji":
        text = random.choice(EMOJI_BY_MOOD.get(mood, ["🌊"]))
    elif style == "short_word":
        text = random.choice(SHORT_WORD_BY_MOOD.get(mood, ["Bugünden."]))
    else:
        text = random.choice(SHORT_SENTENCE_BY_MOOD.get(mood, ["Bugün böyle geçti."]))
    return text, style


def generate_quote_hashtags(recent_sets: list[list[str]] | None = None, n: int | None = None) -> list[str]:
    """3-6 hashtags (point 14), avoiding repeating the same set as recent posts."""
    recent_sets = recent_sets or []
    n = n or random.randint(3, 6)
    pool = list(HASHTAG_POOL)
    random.shuffle(pool)
    candidate = pool[:n]
    for prev in recent_sets:
        overlap = len(set(candidate) & set(prev))
        if prev and overlap / max(len(prev), 1) > 0.7:
            random.shuffle(pool)
            candidate = pool[:n]
    return candidate
