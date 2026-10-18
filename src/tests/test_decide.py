from conftest import det

from censor_proxy.detection import decide


def censored(decisions):
    return [d.detection.label for d in decisions if d.censor]


def test_female_labels_censored(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED"), det("FEMALE_GENITALIA_EXPOSED")], cfg.detection)
    assert censored(ds) == ["FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED"]


def test_male_and_covered_labels_pass(cfg):
    ds = decide([det("MALE_GENITALIA_EXPOSED"), det("MALE_BREAST_EXPOSED"),
                 det("FEMALE_BREAST_COVERED"), det("BELLY_EXPOSED"), det("FACE_FEMALE")],
                cfg.detection)
    assert censored(ds) == []


def test_below_threshold_passes(cfg):
    [d] = decide([det("FEMALE_BREAST_EXPOSED", score=0.49)], cfg.detection)
    assert not d.censor and d.reason == "below threshold 0.5"


def test_unsexed_near_female_censored(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", box=(0, 0, 100, 100)),
                 det("FACE_FEMALE", box=(120, 0, 30, 30))], cfg.detection)
    assert censored(ds) == ["BUTTOCKS_EXPOSED"]
    assert ds[0].reason == "near FACE_FEMALE"


def test_unsexed_near_male_only_passes(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", box=(0, 0, 100, 100)),
                 det("MALE_GENITALIA_EXPOSED", box=(90, 90, 40, 40))], cfg.detection)
    assert censored(ds) == []
    assert ds[0].reason == "near MALE_GENITALIA_EXPOSED only"


def test_unsexed_near_both_censored(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", box=(0, 0, 100, 100)),
                 det("MALE_GENITALIA_EXPOSED", box=(90, 90, 40, 40)),
                 det("FEMALE_GENITALIA_COVERED", box=(-20, 50, 30, 30))], cfg.detection)
    assert censored(ds) == ["BUTTOCKS_EXPOSED"]


def test_unsexed_far_from_male_is_unattributed(cfg):
    ds = decide([det("ANUS_EXPOSED", box=(0, 0, 40, 40)),
                 det("FACE_MALE", box=(500, 500, 40, 40))], cfg.detection)
    assert censored(ds) == ["ANUS_EXPOSED"]
    assert ds[0].reason == "unattributed"


def test_male_exemption_can_be_disabled(make_config):
    cfg = make_config("detection: {male_context_exempts: false}\n")
    ds = decide([det("BUTTOCKS_EXPOSED", box=(0, 0, 100, 100)),
                 det("FACE_MALE", box=(100, 0, 30, 30))], cfg.detection)
    assert censored(ds) == ["BUTTOCKS_EXPOSED"]


def test_weak_context_ignored(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", box=(0, 0, 100, 100)),
                 det("FACE_MALE", score=0.3, box=(100, 0, 30, 30))], cfg.detection)
    assert ds[0].reason == "unattributed"


def test_weak_female_box_next_to_confident_one_censored(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.85, (0, 0, 100, 100)),
                 det("FEMALE_BREAST_EXPOSED", 0.41, (100, 0, 70, 90))], cfg.detection)
    assert [d.censor for d in ds] == [True, True]
    assert ds[1].reason == "female label, corroborated by FEMALE_BREAST_EXPOSED"


def test_corroboration_needs_proximity_and_minimum(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.85, (0, 0, 100, 100)),
                 det("FEMALE_BREAST_EXPOSED", 0.41, (400, 400, 70, 90)),
                 det("FEMALE_BREAST_EXPOSED", 0.30, (100, 0, 70, 90))], cfg.detection)
    assert [d.censor for d in ds] == [True, False, False]


def test_weak_boxes_do_not_corroborate_each_other(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.45, (0, 0, 100, 100)),
                 det("FEMALE_BREAST_EXPOSED", 0.45, (100, 0, 100, 100))], cfg.detection)
    assert censored(ds) == []


def test_lone_female_box_with_male_context_needs_more(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.57, (0, 140, 235, 370)),
                 det("MALE_GENITALIA_EXPOSED", 0.72, (435, 200, 490, 330))], cfg.detection)
    assert censored(ds) == []
    assert ds[0].reason == "below threshold 0.65, male context only"
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.70, (0, 140, 235, 370)),
                 det("MALE_GENITALIA_EXPOSED", 0.72, (435, 200, 490, 330))], cfg.detection)
    assert censored(ds) == ["FEMALE_BREAST_EXPOSED"]


def test_other_female_evidence_keeps_normal_threshold_with_male_present(cfg):
    ds = decide([det("FEMALE_BREAST_EXPOSED", 0.57, (0, 0, 100, 100)),
                 det("FACE_FEMALE", 0.8, (500, 0, 50, 50)),
                 det("MALE_GENITALIA_EXPOSED", 0.72, (300, 300, 100, 100))], cfg.detection)
    assert censored(ds) == ["FEMALE_BREAST_EXPOSED"]


def test_weak_female_inside_unsexed_box_overrides_male_exemption(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", 0.85, (0, 1000, 975, 700)),
                 det("MALE_GENITALIA_EXPOSED", 0.7, (400, 1980, 260, 410)),
                 det("FEMALE_GENITALIA_EXPOSED", 0.26, (420, 1480, 110, 150))], cfg.detection)
    assert censored(ds) == ["BUTTOCKS_EXPOSED"]
    assert ds[0].reason == "overlaps FEMALE_GENITALIA_EXPOSED 0.26"


def test_weak_female_must_overlap_to_override(cfg):
    ds = decide([det("BUTTOCKS_EXPOSED", 0.85, (0, 0, 100, 100)),
                 det("MALE_GENITALIA_EXPOSED", 0.7, (100, 0, 50, 50)),
                 det("FACE_FEMALE", 0.26, (130, 0, 30, 30))], cfg.detection)
    assert censored(ds) == []
