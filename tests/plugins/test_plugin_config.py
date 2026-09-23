from pathlib import Path

import pytest

from conic.plugins.plugin_config import (
    PluginConfigError,
    PluginSetConfig,
    load_plugins_config,
)


def test_parses_bare_string_and_single_key_mapping_entries(tmp_path):
    yaml_text = """
main:
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
    main = cfg["main"]

    assert main.tools[0].name == "read_file"
    assert main.tools[0].params == {}
    assert main.tools[1].name == "bash"
    assert main.tools[1].params == {"timeout": 60}
    assert main.policy[0].name == "permission"
    assert main.policy[1].name == "step_limit"
    assert main.policy[1].params == {"max_steps": 7}
    assert main.summarizer == "default"
    assert main.backend == "openrouter"
    assert main.loop == "react"


def test_defaults_when_sections_omitted(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("main:\n  tools: []\n", encoding="utf-8")

    cfg = load_plugins_config(path)
    main = cfg["main"]

    assert main.tools == []
    assert main.context == []
    assert main.policy == []
    assert main.summarizer == "default"
    assert main.backend == "openrouter"
    assert main.loop == "react"


def test_multiple_named_groups_parse_independently(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text(
        "main:\n  tools: [read_file]\nagent:\n  tools: [bash]\n",
        encoding="utf-8",
    )

    cfg = load_plugins_config(path)

    assert set(cfg.keys()) == {"main", "agent"}
    assert cfg["main"].tools[0].name == "read_file"
    assert cfg["agent"].tools[0].name == "bash"


def test_empty_file_yields_no_groups(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("", encoding="utf-8")

    assert load_plugins_config(path) == {}


def test_invalid_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("main:\n  tools:\n    - [not, a, valid, entry]\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_multi_key_mapping_entry_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text(
        "main:\n  tools:\n    - bash: {timeout: 60}\n      read_file: {}\n", encoding="utf-8"
    )

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_invalid_yaml_syntax_raises_plugin_config_error(tmp_path):
    path = tmp_path / "plugins.yaml"
    path.write_text("main:\n  tools: [unclosed\n", encoding="utf-8")

    with pytest.raises(PluginConfigError):
        load_plugins_config(path)


def test_missing_file_raises_plugin_config_error(tmp_path):
    with pytest.raises(PluginConfigError):
        load_plugins_config(tmp_path / "does-not-exist.yaml")


def test_plugin_set_config_can_be_built_directly_from_a_dict():
    cfg = PluginSetConfig.model_validate({
        "tools": ["read_file", {"bash": {"timeout": 60}}],
    })
    assert cfg.tools[0].name == "read_file"
    assert cfg.tools[1].name == "bash"
    assert cfg.tools[1].params == {"timeout": 60}


def test_repo_default_plugins_yaml_parses_successfully():
    repo_root = Path(__file__).resolve().parents[2]
    cfg = load_plugins_config(repo_root / "config" / "plugins.yaml")

    assert set(cfg.keys()) == {"main", "agent"}

    for group in (cfg["main"], cfg["agent"]):
        tool_names = [spec.name for spec in group.tools]
        assert tool_names == [
            "bash", "read_file", "write_file", "edit_file", "list_skills", "load_skill",
            "web_search", "web_fetch",
        ]
        policy_names = [spec.name for spec in group.policy]
        assert policy_names == ["permission", "step_limit"]
        assert group.summarizer == "default"
        assert group.backend == "openrouter"
        assert group.loop == "react"

    main_context_names = [spec.name for spec in cfg["main"].context]
    assert main_context_names == ["turn_variables", "system_prompt", "truncator", "token_budget", "extra_prompt"]

    agent_context_names = [spec.name for spec in cfg["agent"].context]
    assert agent_context_names == ["turn_variables", "system_prompt", "extra_prompt"]
