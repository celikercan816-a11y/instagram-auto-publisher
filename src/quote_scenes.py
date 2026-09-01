"""Landscape/cityscape/nature scene pool for the "manzara + söz" content
pivot (2026-09-01) -- point 2 of the approved design. No people as the main
subject (a small incidental figure is fine, e.g. a distant silhouette on a
ferry deck, but never the point of the shot). Every prompt explicitly bans
prominent people, readable text, and watermarks, and asks for realistic,
natural-light, true-to-life photography -- point 3.

Each scene is tagged with the moods (see quote_generator.MOODS) it visually
matches, so select_scene_for_mood() can pair a quote with a location that
makes sense (point 8): yalnızlık/sessizlik -> boş bank, gece şehir, sisli göl;
umut -> gün doğumu, açık yol; zaman -> eski sokak, yağmur; yol -> yol, vapur,
uzak manzara; huzur -> deniz, sakin koy, orman.
"""
import random

NEGATIVE_TAIL = (
    "no prominent people, no readable text, no handwriting, no signage with "
    "text, no logos, no watermark, realistic photography, natural light, "
    "true-to-life colors, not oversaturated, no glossy CGI/3D render look, "
    "believable amateur or editorial photography"
)

LANDSCAPE_SCENES = [
    {"id": "bogaz_gunduz", "en": "the Bosphorus strait in Istanbul on a calm day, water and hills across the strait", "moods": ["huzur", "yol"]},
    {"id": "bogaz_gece", "en": "the Bosphorus at night, a suspension bridge lit up, city lights reflecting on dark water", "moods": ["yalnizlik", "melankoli"]},
    {"id": "kiz_kulesi", "en": "a small stone tower on an islet in the Bosphorus at dusk, calm water around it", "moods": ["melankoli", "huzur"]},
    {"id": "vapur_guverte", "en": "the empty deck of an Istanbul ferry crossing the Bosphorus, railing and wake visible, no prominent people", "moods": ["yol", "zaman"]},
    {"id": "gece_istanbul_cati", "en": "a wide view of Istanbul's skyline at night from a rooftop, warm scattered lights", "moods": ["yalnizlik", "melankoli"]},
    {"id": "eski_sokak_gunduz", "en": "an old cobblestone Istanbul side street with historic buildings, soft daylight", "moods": ["zaman"]},
    {"id": "eski_sokak_gece", "en": "an old narrow city street at night, a single warm streetlamp, wet cobblestones", "moods": ["zaman", "yalnizlik"]},
    {"id": "galata_sokak", "en": "a steep narrow street near a historic stone tower in Istanbul, old buildings on both sides", "moods": ["zaman", "melankoli"]},
    {"id": "sahil_gunduz", "en": "a calm seaside promenade with a low stone wall, sea on one side", "moods": ["huzur"]},
    {"id": "deniz_ufuk", "en": "a wide open sea horizon at golden hour, calm water, no land in sight", "moods": ["huzur", "umut"]},
    {"id": "ege_koy", "en": "a small whitewashed Aegean coastal town with narrow streets, blue sea below", "moods": ["huzur"]},
    {"id": "akdeniz_koy", "en": "a quiet Mediterranean cove with turquoise water and pine-covered hills", "moods": ["huzur"]},
    {"id": "dag_manzara", "en": "a wide mountain valley view from a high overlook, layered ridgelines fading into haze", "moods": ["huzur", "yol"]},
    {"id": "orman_sis", "en": "a quiet pine forest path with soft morning mist between the trees", "moods": ["huzur", "yalnizlik"]},
    {"id": "gol_sis", "en": "a still lake surrounded by hills, thin mist hovering just above the water at dawn", "moods": ["yalnizlik", "huzur"]},
    {"id": "yagmurlu_sehir", "en": "a city street in the rain, wet asphalt reflecting streetlights, no prominent people", "moods": ["zaman", "melankoli"]},
    {"id": "pencere_yagmur", "en": "raindrops on a window pane with a soft blurred city view outside", "moods": ["zaman", "melankoli"]},
    {"id": "karli_sokak", "en": "a quiet snow-covered street with soft falling snow, warm window lights in the background", "moods": ["huzur", "yalnizlik"]},
    {"id": "gun_dogumu_yol", "en": "an open empty road stretching toward the horizon at sunrise, soft warm light", "moods": ["umut", "yol"]},
    {"id": "gun_batimi_iskele", "en": "a wooden pier stretching into calm water at sunset, warm orange sky", "moods": ["umut", "melankoli"]},
    {"id": "gece_sehir_isiklari", "en": "a distant city skyline at night seen across water, scattered warm and cool lights", "moods": ["yalnizlik", "umut"]},
    {"id": "bos_bank", "en": "a single empty park bench facing the sea, soft overcast light, no people", "moods": ["yalnizlik", "melankoli"]},
    {"id": "acik_yol", "en": "a long open road cutting through a quiet countryside landscape, wide sky above", "moods": ["yol", "umut"]},
    {"id": "sakin_kafe_dis", "en": "the exterior of a quiet small cafe on a European side street, a few outdoor tables, soft daylight, no people", "moods": ["zaman", "huzur"]},
    {"id": "doga_detay_yaprak", "en": "a close-up natural detail of wet leaves and morning dew, soft natural light", "moods": ["huzur", "zaman"]},
]


def select_scene_for_mood(mood: str, avoid_ids: set[str] | None = None) -> dict:
    """Weighted-random pick among scenes tagged with this mood, avoiding
    recently-used scene ids when a matching alternative exists (point 2:
    'aynı sahneyi sürekli tekrar etme')."""
    avoid_ids = avoid_ids or set()
    matching = [s for s in LANDSCAPE_SCENES if mood in s["moods"]]
    if not matching:
        matching = LANDSCAPE_SCENES
    fresh = [s for s in matching if s["id"] not in avoid_ids]
    pool = fresh or matching
    return dict(random.choice(pool))


def build_scene_prompt(scene: dict) -> str:
    return f"Realistic candid iPhone-style photo of {scene['en']}, {NEGATIVE_TAIL}"
