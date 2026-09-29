"""Model-level reasoning capability contracts without touching user Pi config."""
from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from rsi_system.core.reasoning_capabilities import model_capabilities, resolve_level
from rsi_system.core.pi_agent import run_agent
from rsi_system.core.runtime_roles import model_policy
from rsi_system.dashboard.configuration import Configuration


def _root(tmp_path: Path) -> Path:
    path = tmp_path / "deployment" / "rsi"
    path.mkdir(parents=True)
    (path / "research.json").write_text(json.dumps({"models": {}, "thinking": "high"}))
    return tmp_path


def test_pi_map_derives_only_declared_levels_and_nonreasoning_is_off():
    capabilities = model_capabilities({
        "reasoning": True,
        "thinkingLevelMap": {"off": "off", "minimal": None, "high": "high", "max": "max"},
    })
    assert capabilities.levels == ("off", "low", "medium", "high", "max")
    assert model_capabilities({"reasoning": False}).levels == ("off",)
    assert model_capabilities({"id": "unverified"}).levels is None


@pytest.mark.parametrize(("requested", "expected"), [("xhigh", "max"), ("off", "high"), ("max", "max")])
def test_known_capabilities_clamp_without_emitting_unsupported_level(requested, expected):
    capabilities = model_capabilities({"reasoning": True, "thinkingLevelMap": {
        "off": None, "minimal": None, "low": None, "medium": None,
        "high": "high", "xhigh": None, "max": "max",
    }})
    assert resolve_level(requested, capabilities) == expected


def test_unknown_dashboard_model_is_explicit_and_runtime_omits_thinking(tmp_path):
    root = _root(tmp_path)
    config = Configuration(root, config_dir=tmp_path / "private", pi_dir=tmp_path / "pi")
    base = {"name": "provider", "base_url": "https://api.example.test/v1",
            "api": "openai-responses", "models": ["unknown"]}
    saved = config.save_provider(base)
    model = saved["providers"][0]["models"][0]
    assert model["thinking_capability"] == "unknown"
    assert model["thinking_levels"] is None
    config.save_roles({"roles": {"researcher": {"provider": "provider", "model": "unknown"}},
                       "thinking": "xhigh"})
    private = json.loads((tmp_path / "private" / "research.json").read_text())
    assert private["model_capabilities"]["provider"]["unknown"]["known"] is False
    assert model_policy(private, "researcher")["thinking"] is None
    private["thinking"] = "off"
    assert model_policy(private, "researcher")["thinking"] == "off"
    config.save_provider({**base, "model_settings": {
        "unknown": {"thinking_levels": ["off", "high"], "thinking": "high"},
    }})
    model = config.public()["providers"][0]["models"][0]
    assert model["thinking_levels"] == ["off", "high"]
    private = json.loads((tmp_path / "private" / "research.json").read_text())
    assert model_policy({**private, "models": {"researcher": {"provider": "provider", "model": "unknown"}}},
                        "researcher")["thinking"] == "high"


def test_max_and_off_round_trip(tmp_path):
    root = _root(tmp_path)
    config = Configuration(root, config_dir=tmp_path / "private", pi_dir=tmp_path / "pi")
    config.save_provider({"name": "provider", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["m"],
                          "model_settings": {"m": {"thinking_levels": ["off", "max"],
                                                       "thinking": "off"}}})
    model = config.public()["providers"][0]["models"][0]
    assert model["thinking"] == "off"
    assert model["thinking_levels"] == ["off", "max"]
    config.save_roles({"roles": {}, "thinking": "max"})
    assert config.public()["thinking"] == "max"


def test_old_reasoning_true_xhigh_config_is_materialized_for_pi(tmp_path):
    root = _root(tmp_path)
    config = Configuration(root, config_dir=tmp_path / "private", pi_dir=tmp_path / "pi")
    config.save_provider({"name": "provider", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["m"],
                          "model_settings": {"m": {"thinking": "xhigh"}}})
    # The first write is a new unknown model and therefore explicit legacy
    # selection; a subsequent save must retain the native map.
    stored = json.loads(config.models_path.read_text())
    assert stored["providers"]["provider"]["models"][0]["thinkingLevelMap"]["xhigh"] == "xhigh"
    config.save_provider({"name": "provider", "base_url": "https://api.example.test/v1",
                          "api": "openai-responses", "models": ["m"],
                          "model_settings": {"m": {"thinking": "xhigh"}}})
    assert config.public()["providers"][0]["models"][0]["thinking_levels"] == ["xhigh"]


def test_pi_invocation_omits_thinking_for_unknown_model(tmp_path):
    fake = tmp_path / "pi"
    fake.write_text(f"#!{sys.executable}\n" + '''import json,sys
sys.stdin.read()
print(json.dumps({"type":"message_end","message":{"role":"assistant","stopReason":"stop",
"content":[{"type":"text","text":json.dumps({"argv":sys.argv})}]}}))
''')
    fake.chmod(0o755)
    result = run_agent(executable=str(fake), role="researcher", prompt="{}", cwd=tmp_path,
                       output_dir=tmp_path / "out", timeout_seconds=5, thinking=None)
    assert "--thinking" not in result["payload"]["argv"]
