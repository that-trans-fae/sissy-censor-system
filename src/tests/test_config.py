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
    cfg = make_config("detection: {preset: custom, unsexed_labels: [ANUS_EXPOSED]}\n")
    assert cfg.detection.unsexed_labels == {"ANUS_EXPOSED"}


@pytest.mark.parametrize("text", [
    "censor: {stlye: solid}\n",
    "nonsense: {}\n",
    "censor: {style: sparkles}\n",
    "logging: {level: verbose}\n",
    "whitelist: {builtin: ['(']}\n",
    "proxy: {passthrough_hosts: []}\n",  # now whitelist.builtin
    "images: {placeholder_delay_ms: -1}\n",
    "images: {placeholder_reserve_px: -1}\n",
    "detection: {preset: custom, male_score_ratio: -1}\n",
    "detection: {preset: custom, male_priority_overlap: 0}\n",
])
def test_bad_config_rejected(make_config, text):
    with pytest.raises(ConfigError):
        make_config(text)


def test_label_threshold_override(make_config):
    cfg = make_config("detection: {preset: custom, label_thresholds: {FEMALE_BREAST_EXPOSED: 0.7}}\n")
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


def test_presets(make_config):
    strict = make_config("detection: {threshold: 0.9}\n")  # ignored under a preset
    assert strict.detection.preset == "strict" and strict.detection.threshold == 0.3
    lax = make_config("detection: {preset: lax}\n")
    assert lax.detection.female_labels == {"FEMALE_GENITALIA_EXPOSED"}
    custom = make_config("detection: {preset: custom, threshold: 0.9}\n")
    assert custom.detection.threshold == 0.9
    own = make_config("detection:\n  preset: mine\n  presets:\n"
                      "    mine: {description: x, settings: {threshold: 0.7}}\n")
    assert own.detection.threshold == 0.7


def test_presets_merge_by_name(make_config, tmp_path):
    from censor_proxy.config import merged
    many = {f"level{i}": {"description": f"level {i}", "settings": {"threshold": 0.3 + i / 100}}
            for i in range(25)}
    raw = merged({"detection": {"presets": {**many, "lax": None}, "preset": "level24"}})
    names = list(raw["detection"]["presets"])
    assert names[0] == "strict" and "lax" not in names and len(names) == 26
    assert raw["detection"]["threshold"] == 0.54


@pytest.mark.parametrize("text", [
    "detection: {preset: sparkly}\n",
    "detection: {presets: {lax: {settings: {nonsense: 1}}}}\n",
    "detection: {presets: {custom: {settings: {}}}}\n",
    "detection: {presets: {lax: {settings: {preset: strict}}}}\n",
    "model: {available: [{path: /x.onnx}]}\n",
])
def test_bad_presets_and_models_rejected(make_config, text):
    with pytest.raises(ConfigError):
        make_config(text)


def test_model_names(make_config):
    m = make_config().model
    assert m.name_for(None, 320) == "Fast (320n)"
    assert m.name_for(None, 640) == "Fast (320n)"  # null is the bundled 320n
    assert m.name_for("/app/models/640m.onnx", 640) == "Slow (640m)"
    assert m.name_for("/other.onnx", 640) == "/other.onnx"


def test_whitelist_domains_extend_builtin(make_config):
    builtin = make_config().whitelist.builtin
    cfg = make_config("whitelist: {domains: [my-bank.example]}\n")
    wl = cfg.whitelist
    assert wl.builtin == builtin
    assert wl.patterns[:-1] == builtin
    assert re.search(wl.patterns[-1], "login.my-bank.example:443")
    assert not re.search(wl.patterns[-1], "notmy-bank.example:443")
    assert wl.match("login.my-bank.example") == "my-bank.example"
    assert "chase" in wl.match("www.chase.com")
    assert wl.match("i.imgur.com") is None


def _passed_through(cfg, host):
    return any(re.search(p, f"{host}:443") for p in cfg.whitelist.patterns)


@pytest.mark.parametrize("host", [
    "update.googleapis.com", "accounts.google.com", "login.microsoftonline.com",
    "my.1password.com", "js.stripe.com", "www.paypal.com", "secure.chase.com",
    "www.53.com", "client.schwab.com", "sa.www4.irs.gov", "api.id.me",
    "www.nga.gov", "www.army.mil", "www.si.edu", "www.gov.uk", "www.tax.service.gov.uk",
    "www.sydney.edu.au", "mychart.clevelandclinic.org", "www.mychart.com", "www.myuhc.com",
    "www.cvs.com", "www.nhs.uk", "www.hsbc.co.uk", "www.commbank.com.au", "www.equifax.com",
    "us.archive.ubuntu.com", "security.ubuntu.com", "api.snapcraft.io",
    "fastly.cdn.snapcraftcontent.com", "download.windowsupdate.com", "swcdn.apple.com",
    "www.crunchyroll.com", "www.disneyplus.com", "ipv4-c001.nflxvideo.net", "play.max.com",
    "tv.apple.com", "pluto.tv",
])
def test_builtin_whitelist_covers(make_config, host):
    assert _passed_through(make_config(), host)


# Hosts that serve images people browse; passing any of them through would
# let those images skip the censor.
@pytest.mark.parametrize("host", [
    "www.google.com", "encrypted-tbn0.gstatic.com", "lh3.googleusercontent.com",
    "www.paypalobjects.com", "i.imgur.com", "cdn.discordapp.com",
    "chase.com.example.net", "notchase.com", "gov.example.com", "edu.example.net",
    "gov.uk.example.com", "edu.au.example.net", "mychartxyz.com", "www.ubuntu.com",
    "launchpad.net", "snapcraft.io", "www.amazon.com", "m.media-amazon.com",
    "www.apple.com",
])
def test_builtin_whitelist_leaves_image_hosts(make_config, host):
    assert not _passed_through(make_config(), host)
