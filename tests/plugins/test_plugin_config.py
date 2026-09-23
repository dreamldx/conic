from pathlib import Path

import pytest

from conic.plugins.plugin_config import (
    PluginConfigError,
    PluginsConfig,
    load_plugins_config,
)


def test_parses_bare_string_and_single_key_mapping_entries(tmp_path):
    yaml_text = """
tools:
  - read_file
  - bash: {timeout: 60}
context: []
policy:
  - permission
  - step_limit: {max_steps: 7}
summarizer: default
backend: openrouter
"""
    path = tmp_path / "plugins.yaml"
    path.write_text(yaml_text, encoding="utf-8")

    cfg = load_plugins_config(path)

    assert cfg.tools[0].name == "read_file"
    assert cfg.tools[0].params == {}
    assert cfg.tools[1].name == "bash"
    assert cfg.tools[1].params == {"timeout": 60}
    assert cfg.policy[0].name == "permission"
    assert cfg.policy[1].name == "step_limit"
    assert cfg.policy[1].params == {"max_steps": 7}
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"


def test_defaults_when_sections_omitted(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools: []\n", encoding="utf-8")

    cfg = load_plugins_config(path)

    assert cfg.tools == []
    assert cfg.context == []
    assert cfg.policy == []
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"


def test_invalid_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools:\n  - [not, a, valid, entry]\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_multi_key_mapping_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools:\n  - bash: {timeout: 60}\n    read_file: {}\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_invalid_yaml_syntax_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("tools: [unclosed\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_missing_file_raises_plugin_config_error(tmp_path):
    with pytest.raises(PluginConfigError):
        load_plugins_config(tmp_path / "does-not-exist.yaml")


def test_plugins_config_can_be_built_directly_from_a_dict():
    cfg = PluginsConfig.model_validate({
        "tools": ["read_file", {"bash": {"timeout": 60}}],
    })
    assert cfg.tools[0].name == "read_file"
    assert cfg.tools[1].name == "bash"
    assert cfg.tools[1].params == {"timeout": 60}


def test_repo_default_plugins_yaml_parses_successfully():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_plugins_config(repo_root / "plugins.yaml")

    tool_names = [spec.name for spec in cfg.tools]
    assert tool_names == [
        "bash", "read_file", "write_file", "edit_file", "list_skills", "load_skill",
        "web_search", "web_fetch",
    ]
    context_names = [spec.name for spec in cfg.context]
    assert context_names == ["turn_variables", "system_prompt", "truncator", "token_budget", "extra_prompt"]
    policy_names = [spec.name for spec in cfg.policy]
    assert policy_names == ["permission", "step_limit"]
    assert cfg.summarizer == "default"
    assert cfg.backend == "openrouter"
