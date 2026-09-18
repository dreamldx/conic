import sys

import pytest

from conic.config import Config
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.plugins.models.openrouter import OpenRouterModelPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.registry import _detect_shell, build_plugin_set
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.skills import ListSkillsToolPlugin, LoadSkillToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def make_config():
    return Config(
        _env_file=None,
        PROJECT_ROOT="/tmp",
        DISCORD_BOT_TOKEN="d", OPENROUTER_API_KEY="k", OPENROUTER_MODEL="test-model",
        WORKSPACE_ROOT="./workspace", DUCKDB_PATH="./data/conic.duckdb",
        LOG_LEVEL="DEBUG", MAX_STEPS_PER_TURN=7, CONTEXT_TOKEN_BUDGET=123, TRUNCATE_KEEP_LAST_N=9,
        BASH_TIMEOUT=42,
    )


def test_build_plugin_set_wires_the_v1_tools():
    plugin_set = build_plugin_set(make_config())
    assert issubclass(plugin_set.tool_classes[0], BashToolPlugin)
    assert plugin_set.tool_classes[1:4] == (ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin)
    assert issubclass(plugin_set.tool_classes[4], ListSkillsToolPlugin)
    assert issubclass(plugin_set.tool_classes[5], LoadSkillToolPlugin)


def test_build_plugin_set_wires_skill_tools_with_project_root():
    plugin_set = build_plugin_set(make_config())
    list_cls = next(c for c in plugin_set.tool_classes if issubclass(c, ListSkillsToolPlugin))
    load_cls = next(c for c in plugin_set.tool_classes if issubclass(c, LoadSkillToolPlugin))

    list_tool = list_cls(workspace_dir="/tmp/ws")
    load_tool = load_cls(workspace_dir="/tmp/ws")

    assert list_tool._workspace_dir == "/tmp/ws"
    assert list_tool._project_root == "/tmp"
    assert load_tool._workspace_dir == "/tmp/ws"
    assert load_tool._project_root == "/tmp"


def test_build_plugin_set_wires_skills_section_plugin():
    from conic.plugins.context.sections.skills import SkillsSectionPlugin

    plugin_set = build_plugin_set(make_config())
    system_prompt_plugin = plugin_set.context_plugins[1]("/tmp/ws", [])
    kinds = [type(p) for p in system_prompt_plugin._section_plugins]
    assert SkillsSectionPlugin in kinds


def test_build_plugin_set_wires_bash_tool_with_configured_timeout():
    plugin_set = build_plugin_set(make_config())
    bash_cls = plugin_set.tool_classes[0]
    assert bash_cls.llm_name == "bash"
    assert bash_cls.schema == BashToolPlugin.schema

    bash_tool = bash_cls(workspace_dir="/tmp/ws")
    assert bash_tool._timeout == 42


def test_build_plugin_set_wires_backend_with_configured_model():
    plugin_set = build_plugin_set(make_config())
    backend = plugin_set.backend("discord:1")
    assert isinstance(backend, OpenRouterModelPlugin)
    assert backend.model == "test-model"


def test_build_plugin_set_wires_backend_with_the_sessions_id_for_sticky_routing():
    plugin_set = build_plugin_set(make_config())
    backend = plugin_set.backend("discord:123")
    assert backend._session_id == "discord:123"


def test_build_plugin_set_wires_backend_without_blacklist_by_default():
    plugin_set = build_plugin_set(make_config())
    backend = plugin_set.backend("discord:1")
    assert backend._provider_blacklist == []


def test_build_plugin_set_parses_configured_provider_blacklist():
    config = Config(
        _env_file=None,
        PROJECT_ROOT="/tmp",
        DISCORD_BOT_TOKEN="d", OPENROUTER_API_KEY="k", OPENROUTER_MODEL="test-model",
        OPENROUTER_PROVIDER_BLACKLIST="novita, together ,,",
        WORKSPACE_ROOT="./workspace", DUCKDB_PATH="./data/conic.duckdb",
        LOG_LEVEL="DEBUG", MAX_STEPS_PER_TURN=7, CONTEXT_TOKEN_BUDGET=123, TRUNCATE_KEEP_LAST_N=9,
        BASH_TIMEOUT=42,
    )
    plugin_set = build_plugin_set(config)
    backend = plugin_set.backend("discord:1")
    assert backend._provider_blacklist == ["novita", "together"]


def test_build_plugin_set_wires_config_project_name_as_backend_app_name():
    config = make_config()
    plugin_set = build_plugin_set(config)
    backend = plugin_set.backend("discord:1")
    assert backend._app_name == config.project_name


def test_build_plugin_set_backend_factory_produces_fresh_instances_sharing_one_client():
    plugin_set = build_plugin_set(make_config())
    backend1 = plugin_set.backend("discord:1")
    backend2 = plugin_set.backend("discord:2")
    assert backend1 is not backend2
    assert backend1._client is backend2._client


def test_build_plugin_set_wires_context_chain_with_configured_values():
    plugin_set = build_plugin_set(make_config())
    instances = [factory("/tmp/ws", []) for factory in plugin_set.context_plugins]
    kinds = [type(p) for p in instances]
    assert kinds == [
        TurnVariableUpdaterPlugin, SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin, ExtraPromptPlugin,
    ]
    truncator = instances[2]
    assert truncator._keep_last_n == 9
    token_budget = instances[3]
    assert token_budget._budget_tokens == 123


def test_build_plugin_set_wires_turn_variable_updater_plugin():
    plugin_set = build_plugin_set(make_config())
    variables_plugin = plugin_set.context_plugins[0]("/tmp/ws", [])
    assert isinstance(variables_plugin, TurnVariableUpdaterPlugin)


def test_build_plugin_set_wires_policy_plugins_with_configured_max_steps():
    plugin_set = build_plugin_set(make_config())
    instances = [factory() for factory in plugin_set.policy_plugins]
    kinds = [type(p) for p in instances]
    assert kinds == [PermissionPolicyPlugin, StepLimitPlugin]
    step_limit = instances[1]
    assert step_limit._max_steps == 7


def test_build_plugin_set_wires_summarizer():
    plugin_set = build_plugin_set(make_config())
    assert isinstance(plugin_set.summarizer(), SummarizerPlugin)


def test_build_plugin_set_context_and_policy_factories_produce_fresh_instances():
    plugin_set = build_plugin_set(make_config())
    for factory in plugin_set.context_plugins:
        assert factory("/tmp/ws", []) is not factory("/tmp/ws", [])
    for factory in plugin_set.policy_plugins:
        assert factory() is not factory()
    assert plugin_set.summarizer() is not plugin_set.summarizer()


def test_loop_factory_produces_a_react_loop_plugin():
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert isinstance(loop, ReactLoopPlugin)


def test_loop_factory_wires_session_and_global_variables():
    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 0, "turn_count": 0}
    assert loop._global_variables["model"] == "test-model"
    assert "platform" in loop._global_variables
    assert loop._global_variables["timezone"] == "UTC"


def test_loop_factory_seeds_session_variables_from_persisted_values():
    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {"tokens_used": 999})
    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 999, "turn_count": 0}


def test_build_plugin_set_merges_caller_supplied_global_variables():
    plugin_set = build_plugin_set(make_config(), global_variables={"deployment": "staging"})
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._global_variables["deployment"] == "staging"
    assert loop._global_variables["model"] == "test-model"


def test_build_plugin_set_wires_the_detected_shell_into_global_variables():
    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._global_variables["shell"] == _detect_shell()


@pytest.mark.skipif(sys.platform != "win32", reason="Path parses ComSpec backslashes only on Windows")
def test_detect_shell_uses_comspec_basename_on_windows(monkeypatch):
    monkeypatch.setattr("conic.plugins.registry.platform.system", lambda: "Windows")
    monkeypatch.setenv("ComSpec", r"C:\Windows\System32\cmd.exe")
    assert _detect_shell() == "cmd.exe"


@pytest.mark.skipif(sys.platform != "win32", reason="Path parses ComSpec backslashes only on Windows")
def test_detect_shell_falls_back_to_default_comspec_when_unset_on_windows(monkeypatch):
    monkeypatch.setattr("conic.plugins.registry.platform.system", lambda: "Windows")
    monkeypatch.delenv("ComSpec", raising=False)
    assert _detect_shell() == "cmd.exe"


def test_detect_shell_is_bin_bash_on_macos(monkeypatch):
    monkeypatch.setattr("conic.plugins.registry.platform.system", lambda: "Darwin")
    assert _detect_shell() == "/bin/bash"


def test_detect_shell_is_bin_sh_on_posix(monkeypatch):
    monkeypatch.setattr("conic.plugins.registry.platform.system", lambda: "Linux")
    assert _detect_shell() == "/bin/sh"


from conic.plugins.tools.web_fetch import WebFetchToolPlugin, build_web_fetch_schema
from conic.plugins.tools.web_search import WebSearchToolPlugin


def make_config_with(**extra):
    base = {
        "_env_file": None,
        "PROJECT_ROOT": "/tmp",
        "DISCORD_BOT_TOKEN": "d",
        "OPENROUTER_API_KEY": "k",
        "OPENROUTER_MODEL": "test-model",
        "WORKSPACE_ROOT": "./workspace",
        "DUCKDB_PATH": "./data/conic.duckdb",
        "LOG_LEVEL": "DEBUG",
        "MAX_STEPS_PER_TURN": 7,
        "CONTEXT_TOKEN_BUDGET": 123,
        "TRUNCATE_KEEP_LAST_N": 9,
        "BASH_TIMEOUT": 42,
    }
    base.update(extra)
    return Config(**base)


def test_web_tools_absent_without_api_keys():
    plugin_set = build_plugin_set(make_config())
    for cls in plugin_set.tool_classes:
        assert not issubclass(cls, (WebSearchToolPlugin, WebFetchToolPlugin))


def test_web_search_registered_with_tavily_key_only():
    plugin_set = build_plugin_set(make_config_with(TAVILY_API_KEY="tv", WEB_SEARCH_TIMEOUT=11))
    search_classes = [c for c in plugin_set.tool_classes if issubclass(c, WebSearchToolPlugin)]
    assert len(search_classes) == 1
    assert not any(issubclass(c, WebFetchToolPlugin) for c in plugin_set.tool_classes)
    tool = search_classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "tv"
    assert tool._timeout == 11


def test_web_fetch_registered_with_firecrawl_key_and_raw_schema_by_default():
    plugin_set = build_plugin_set(make_config_with(FIRECRAWL_API_KEY="fc", WEB_FETCH_MAX_CHARS=5000))
    fetch_classes = [c for c in plugin_set.tool_classes if issubclass(c, WebFetchToolPlugin)]
    assert len(fetch_classes) == 1
    assert fetch_classes[0].schema == build_web_fetch_schema(include_prompt=False)
    tool = fetch_classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "fc"
    assert tool._max_chars == 5000
    assert tool._summary_client is None


def test_web_fetch_summary_model_enables_prompt_param_and_shared_client():
    plugin_set = build_plugin_set(make_config_with(
        FIRECRAWL_API_KEY="fc", WEB_FETCH_SUMMARY_MODEL="fast-model",
    ))
    fetch_cls = next(c for c in plugin_set.tool_classes if issubclass(c, WebFetchToolPlugin))
    assert fetch_cls.schema == build_web_fetch_schema(include_prompt=True)
    tool = fetch_cls(workspace_dir="/tmp/ws")
    assert tool._summary_model == "fast-model"
    assert tool._summary_client is not None
