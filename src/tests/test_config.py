import re

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
    "images: {placeholder_delay_ms: -1}\n",
    "images: {placeholder_reserve_px: -1}\n",
    "proxy: {extra_passthrough_hosts: ['(']}\n",
    "detection: {male_score_ratio: -1}\n",
    "detection: {male_priority_overlap: 0}\n",
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


def test_extra_passthrough_hosts_extend_defaults(make_config):
    defaults = make_config().proxy.passthrough_hosts
    cfg = make_config("proxy: {extra_passthrough_hosts: ['^bank\\.example:']}\n")
    assert cfg.proxy.passthrough_hosts == defaults
    assert cfg.proxy.ignore_hosts == defaults + ("^bank\\.example:",)


def _passed_through(cfg, host):
    return any(re.search(p, f"{host}:443") for p in cfg.proxy.ignore_hosts)


@pytest.mark.parametrize("host", [
    "update.googleapis.com", "accounts.google.com", "login.microsoftonline.com",
    "my.1password.com", "js.stripe.com", "www.paypal.com", "secure.chase.com",
    "www.53.com", "client.schwab.com", "sa.www4.irs.gov", "api.id.me",
    "www.nga.gov", "www.army.mil", "www.si.edu",
])
def test_default_passthrough_covers(make_config, host):
    assert _passed_through(make_config(), host)


# Hosts that serve images people browse; passing any of them through would
# let those images skip the censor.
@pytest.mark.parametrize("host", [
    "www.google.com", "encrypted-tbn0.gstatic.com", "lh3.googleusercontent.com",
    "www.paypalobjects.com", "i.imgur.com", "cdn.discordapp.com",
    "chase.com.example.net", "notchase.com", "gov.example.com", "edu.example.net",
])
def test_default_passthrough_leaves_image_hosts(make_config, host):
    assert not _passed_through(make_config(), host)
