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
    assert cfg.detection.threshold_for("FEMALE_GENITALIA_EXPOSED") == cfg.detection.threshold


def test_data_uri_content_types_normalised(make_config):
    cfg = make_config("data_uris: {content_types: [Text/HTML]}\n")
    assert cfg.data_uris.content_types == {"text/html"}


def test_whitelist_matches_domain_and_subdomains(make_config):
    cfg = make_config("whitelist: {domains: [www.YouTube.com, '*.example.org.', 'https://ytimg.com/x']}\n")
    wl = cfg.whitelist
    assert wl.domains == ("youtube.com", "example.org", "ytimg.com")
    assert wl.match("youtube.com") == "youtube.com"
    assert wl.match("WWW.youtube.com.") == "youtube.com"
    assert wl.match("i.ytimg.com") == "ytimg.com"
    assert wl.match("notyoutube.com") is None
    assert wl.match("youtube.com.evil.net") is None


@pytest.mark.parametrize("text", [
    "whitelist: {domains: ['not a domain']}\n",
    "admin: {port: 8081}\n",  # same as proxy.internal_port
    "admin: {nonsense: 1}\n",
])
def test_bad_admin_config_rejected(make_config, text):
    with pytest.raises(ConfigError):
        make_config(text)
