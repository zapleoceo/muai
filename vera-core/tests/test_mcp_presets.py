from app.mcp.presets import PRESETS, find


def test_presets_have_expected_ids():
    ids = {p["id"] for p in PRESETS}
    assert {"fetch", "git", "filesystem", "memory", "github",
            "instagram", "facebook"} <= ids


def test_each_preset_well_formed():
    for p in PRESETS:
        assert p["transport"] == "stdio"
        assert isinstance(p["command"], list) and p["command"]
        assert isinstance(p["env_required"], list)
        assert p["label"] and p["description"]


def test_find_unknown_returns_none():
    assert find("nonexistent") is None
    assert find("fetch")["id"] == "fetch"
