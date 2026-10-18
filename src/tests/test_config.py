import pytest

from censor_proxy.config import ConfigError


def test_defaults_load(make_config):
    cfg = make_config()
    assert cfg.censor.style == "gaussian"
    assert cfg.logging.level == "info"
    assert "FEMALE_BREAST_EXPOSED" in cfg.detection.female_labels


def test_override_merges_within_section(make_config):
    cfg = make_config("censor: {style: pixelate}\n")
    assert cfg.censor.style == "pixelate"
    assert cfg.censor.padding == 0.10


def test_lists_replace_wholesale(make_config):
    cfg = make_config("detection: {unsexed_labels: [ANUS_EXPOSED]}\n")
    assert cfg.detection.unsexed_labels == {"ANUS_EXPOSED"}


@pytest.mark.parametrize("text", [
    "censor: {stlye: solid}\n",
    "nonsense: {}\n",
    "censor: {style: sparkles}\n",
    "logging: {level: verbose}\n",
    "proxy: {passthrough_hosts: ['(']}\n",
])
def test_bad_config_rejected(make_config, text):
    with pytest.raises(ConfigError):
        make_config(text)


def test_label_threshold_override(make_config):
    cfg = make_config("detection: {label_thresholds: {FEMALE_BREAST_EXPOSED: 0.7}}\n")
    assert cfg.detection.threshold_for("FEMALE_BREAST_EXPOSED") == 0.7
    assert cfg.detection.threshold_for("FEMALE_GENITALIA_EXPOSED") == 0.5
