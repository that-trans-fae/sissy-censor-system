import json

from censor_proxy import config, updates
from censor_proxy.config import from_override, migrate


def test_policy_lines(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text("updates:\n  enabled: false\n")
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    assert updates.policy_lines() == ["enabled=0", "wait_for_browser=1"]


def test_policy_reports_a_broken_config(tmp_path, monkeypatch, capsys):
    path = tmp_path / "config.yaml"
    path.write_text("updates:\n  enabled: maybe\n  nope: 1\n")
    monkeypatch.setenv(config.CONFIG_ENV, str(path))
    assert updates.main(["policy"]) == 1
    assert capsys.readouterr().out == ""
    assert updates.main(["nope"]) == 2


def test_read_status(tmp_path):
    path = tmp_path / "update.json"
    assert updates.read_status(path) is None
    path.write_text(json.dumps({"state": "updated", "message": "Updated to 1.1.0."}))
    assert updates.read_status(path)["state"] == "updated"
    path.write_text("[1, 2]")
    assert updates.read_status(path) is None
    path.write_text("{not json")
    assert updates.read_status(path) is None


def test_version(monkeypatch):
    monkeypatch.delenv(updates.VERSION_ENV, raising=False)
    assert updates.version() == "dev"
    monkeypatch.setenv(updates.VERSION_ENV, "1.2.0")
    assert updates.version() == "1.2.0"


def test_migrate_renames_and_drops_keys():
    rules = {"censor.blur": "censor.style", "video.old": None, "legacy": None,
             "images.moved": "cache.max_entries"}
    override = {"censor": {"blur": "solid"}, "video": {"old": 1, "enabled": False},
                "legacy": {"x": 1}, "images": {"moved": 7}, "cache": {"max_entries": 9}}
    out = migrate(override, rules)
    assert out == {"censor": {"style": "solid"}, "video": {"enabled": False},
                   "cache": {"max_entries": 9}}
    # The caller's mapping is left alone.
    assert override["censor"] == {"blur": "solid"}


def test_migrations_apply_before_unknown_keys_are_rejected(monkeypatch):
    monkeypatch.setattr(config, "MIGRATIONS", {"censor.blur": "censor.style", "old_section": None})
    cfg = from_override({"censor": {"blur": "solid"}, "old_section": {"a": 1}})
    assert cfg.censor.style == "solid"


def test_dropped_keys_still_load():
    from_override({"video": {"link_previews": False}})


def test_migrations_point_at_real_keys():
    defaults = config.defaults()
    for old, new in config.MIGRATIONS.items():
        section, _, key = old.partition(".")
        assert key not in defaults.get(section, {}) if key else section not in defaults, old
        if new:
            section, _, key = new.partition(".")
            assert key in defaults[section] if key else section in defaults, new
