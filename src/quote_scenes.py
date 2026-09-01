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

AI_GIBBERISH mitigation (2026-09-01, point 1 of the post-launch fixes): the
"sakin_kafe_dis" (quiet cafe exterior) scene was REMOVED entirely -- a live
test after strengthening NEGATIVE_TAIL still produced a large, prominent,
fully-readable fake shop sign ("Café Cllene", twice) on this exact scene,
while 4 other previously-flagged scene types (mountain valley, seaside
promenade, rooftop night skyline, Aegean street) came back clean under the
same stronger prompt. "Cafe" as a concept appears to reliably pull a sign
out of the model regardless of negative prompting; every other scene here
stayed. Remaining "signage_risk: high" scenes (urban street-level scenes
that still carry some risk) are downweighted, not excluded -- see
_SIGNAGE_RISK_WEIGHT below -- since a real incident this session also hit a
"low risk" open mountain scene with zero built structures, so scene choice
alone is a mitigation, not a guarantee (see quote_quality.
check_possible_text_artifact() for the complementary, honestly-imperfect
heuristic check).
"""
import random

NEGATIVE_TAIL = (
    "no readable text of any kind, no letters, no words, no typography, no signs, "
    "no signage, no storefront lettering, no shop names, no street signs, "
    "no advertisements, no billboards, no posters, no logos, no brand names, "
    "no handwriting, no watermark, no prominent people, realistic photography, "
    "natural light, true-to-life colors, not oversaturated, no glossy CGI/3D "
    "render look, believable amateur or editorial photography"
)

# signage_risk: "low" (open water/sky/nature/distant skyline -- no built
# commercial structures close enough to carry legible signage) vs "high"
# (close-up urban street/storefront scenes where a diffusion model has real
# opportunity to hallucinate shop names/signs -- point 1 of the 2026-09-01
# AI_GIBBERISH mitigation). This is a bias, not a guarantee: a real
# incident this session hallucinated large fake text over an open mountain
# valley (dag_manzara, "low" risk) with nothing signage-like in the prompt
# at all -- diffusion models can add text artifacts anywhere. Combined with
# the strengthened NEGATIVE_TAIL and quote_quality's heuristic
# check_possible_text_artifact(), never relied on alone.
LANDSCAPE_SCENES = [
    {"id": "bogaz_gunduz", "en": "the Bosphorus strait in Istanbul on a calm day, water and hills across the strait", "moods": ["huzur", "yol"], "signage_risk": "low"},
    {"id": "bogaz_gece", "en": "the Bosphorus at night, a suspension bridge lit up, city lights reflecting on dark water, no illuminated shop signs or billboards", "moods": ["yalnizlik", "melankoli"], "signage_risk": "low"},
    {"id": "kiz_kulesi", "en": "a small stone tower on an islet in the Bosphorus at dusk, calm water around it", "moods": ["melankoli", "huzur"], "signage_risk": "low"},
    {"id": "vapur_guverte", "en": "the empty deck of an Istanbul ferry crossing the Bosphorus, railing and wake visible, no prominent people", "moods": ["yol", "zaman"], "signage_risk": "low"},
    {"id": "gece_istanbul_cati", "en": "a wide view of Istanbul's skyline at night from a rooftop, warm scattered lights, distant buildings with no legible signs or billboards", "moods": ["yalnizlik", "melankoli"], "signage_risk": "high"},
    {"id": "eski_sokak_gunduz", "en": "an old cobblestone Istanbul side street with historic buildings, soft daylight, blank unmarked building facades", "moods": ["zaman"], "signage_risk": "high"},
    {"id": "eski_sokak_gece", "en": "an old narrow city street at night, a single warm streetlamp, wet cobblestones, blank unmarked building facades", "moods": ["zaman", "yalnizlik"], "signage_risk": "high"},
    {"id": "galata_sokak", "en": "a steep narrow street near a historic stone tower in Istanbul, old buildings on both sides, blank unmarked facades", "moods": ["zaman", "melankoli"], "signage_risk": "high"},
    {"id": "sahil_gunduz", "en": "a calm seaside promenade with a low stone wall, sea on one side, empty of any signs or plaques", "moods": ["huzur"], "signage_risk": "high"},
    {"id": "deniz_ufuk", "en": "a wide open sea horizon at golden hour, calm water, no land in sight", "moods": ["huzur", "umut"], "signage_risk": "low"},
    {"id": "ege_koy", "en": "a small whitewashed Aegean coastal town with narrow streets, blue sea below, blank unmarked walls", "moods": ["huzur"], "signage_risk": "high"},
    {"id": "akdeniz_koy", "en": "a quiet Mediterranean cove with turquoise water and pine-covered hills", "moods": ["huzur"], "signage_risk": "low"},
    {"id": "dag_manzara", "en": "a wide mountain valley view from a high overlook, layered ridgelines fading into haze", "moods": ["huzur", "yol"], "signage_risk": "low"},
    {"id": "orman_sis", "en": "a quiet pine forest path with soft morning mist between the trees", "moods": ["huzur", "yalnizlik"], "signage_risk": "low"},
    {"id": "gol_sis", "en": "a still lake surrounded by hills, thin mist hovering just above the water at dawn", "moods": ["yalnizlik", "huzur"], "signage_risk": "low"},
    {"id": "yagmurlu_sehir", "en": "a city street in the rain, wet asphalt reflecting streetlights, no prominent people, distant blurred buildings with no legible signs", "moods": ["zaman", "melankoli"], "signage_risk": "high"},
    {"id": "pencere_yagmur", "en": "raindrops on a window pane with a soft blurred city view outside", "moods": ["zaman", "melankoli"], "signage_risk": "low"},
    {"id": "karli_sokak", "en": "a quiet snow-covered street with soft falling snow, warm window lights in the background, blank unmarked facades", "moods": ["huzur", "yalnizlik"], "signage_risk": "high"},
    {"id": "gun_dogumu_yol", "en": "an open empty road stretching toward the horizon at sunrise, soft warm light", "moods": ["umut", "yol"], "signage_risk": "low"},
    {"id": "gun_batimi_iskele", "en": "a wooden pier stretching into calm water at sunset, warm orange sky", "moods": ["umut", "melankoli"], "signage_risk": "low"},
    {"id": "gece_sehir_isiklari", "en": "a distant city skyline at night seen across water, scattered warm and cool lights, too far away for any sign to be legible", "moods": ["yalnizlik", "umut"], "signage_risk": "low"},
    {"id": "bos_bank", "en": "a single empty park bench facing the sea, soft overcast light, no people", "moods": ["yalnizlik", "melankoli"], "signage_risk": "low"},
    {"id": "acik_yol", "en": "a long open road cutting through a quiet countryside landscape, wide sky above", "moods": ["yol", "umut"], "signage_risk": "low"},
    {"id": "doga_detay_yaprak", "en": "a close-up natural detail of wet leaves and morning dew, soft natural light", "moods": ["huzur", "zaman"], "signage_risk": "low"},
]

_SIGNAGE_RISK_WEIGHT = {"low": 1.0, "high": 0.4}


def select_scene_for_mood(mood: str, avoid_ids: set[str] | None = None) -> dict:
    """Weighted-random pick among scenes tagged with this mood, avoiding
    recently-used scene ids when a matching alternative exists (point 2:
    'aynı sahneyi sürekli tekrar etme'), and favoring (not excluding --
    variety still matters) low signage-risk scenes per point 1's
    AI_GIBBERISH mitigation."""
    avoid_ids = avoid_ids or set()
    matching = [s for s in LANDSCAPE_SCENES if mood in s["moods"]]
    if not matching:
        matching = LANDSCAPE_SCENES
    fresh = [s for s in matching if s["id"] not in avoid_ids]
    pool = fresh or matching
    weights = [_SIGNAGE_RISK_WEIGHT.get(s.get("signage_risk", "low"), 1.0) for s in pool]
    return dict(random.choices(pool, weights=weights, k=1)[0])


def build_scene_prompt(scene: dict) -> str:
    return f"Realistic candid iPhone-style photo of {scene['en']}, {NEGATIVE_TAIL}"
