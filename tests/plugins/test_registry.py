import sys

import pytest

from conic.config import Config
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
from conic.plugins.context.sections.skills import SkillsSectionPlugin
from conic.plugins.context.sections.tooling import ToolingSectionPlugin
from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.plugins.models.openrouter import OpenRouterModelPlugin
from conic.plugins.plugin_config import PluginConfigError, PluginSpec
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.registry import (
    BuildContext,
    _build_backend,
    _build_context_plugins,
    _build_policy_plugins,
    _build_sections,
    _build_summarizer,
    _build_tool_classes,
    _detect_shell,
    build_plugin_set,
)
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.web_fetch import build_web_fetch_schema
from conic.plugins.tools.write_file import WriteFileToolPlugin


def make_build_context(config):
    from openai import AsyncOpenAI
    client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)
    return BuildContext(shared_client=client, prompts={})


DEFAULT_PLUGINS_YAML = """
main:
  tools:
    - bash: {timeout: 42}
    - read_file
    - write_file
    - edit_file
    - list_skills
    - load_skill
  context:
    - turn_variables
    - system_prompt: {sections: [identity, tooling, skills, workspace, runtime, execution]}
    - truncator: {keep_last_n: 9}
    - token_budget: {budget_tokens: 123}
    - extra_prompt: {sections: [dynamic_state]}
  policy:
    - permission
    - step_limit: {max_steps: 7}
  summarizer: default
  backend: openrouter
"""


def make_config(tmp_path, yaml_text=DEFAULT_PLUGINS_YAML, **extra):
    plugins_yaml = tmp_path / "plugins.yaml"
    plugins_yaml.write_text(yaml_text, encoding="utf-8")
    base = {
        "_env_file": None,
        "PROJECT_ROOT": "/tmp",
        "DISCORD_BOT_TOKEN": "d", "OPENROUTER_API_KEY": "k", "OPENROUTER_MODEL": "test-model",
        "WORKSPACE_ROOT": "./workspace", "DUCKDB_PATH": "./data/conic.duckdb",
        "LOG_LEVEL": "DEBUG",
        "PLUGINS_CONFIG_PATH": str(plugins_yaml),
    }
    base.update(extra)
    return Config(**base)


def test_build_plugin_set_backend_factory_produces_fresh_instances_sharing_one_client(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    backend1 = plugin_set.backend("discord:1")
    backend2 = plugin_set.backend("discord:2")
    assert backend1 is not backend2
    assert backend1._client is backend2._client


def test_build_plugin_set_context_and_policy_factories_produce_fresh_instances(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    for factory in plugin_set.context_plugins:
        assert factory("/tmp/ws", []) is not factory("/tmp/ws", [])
    for factory in plugin_set.policy_plugins:
        assert factory() is not factory()
    assert plugin_set.summarizer() is not plugin_set.summarizer()


def test_loop_factory_produces_a_react_loop_plugin(tmp_path):
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert isinstance(loop, ReactLoopPlugin)


def test_loop_factory_wires_session_and_global_variables(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 0, "turn_count": 0}
    assert loop._global_variables["model"] == "test-model"
    assert "platform" in loop._global_variables
    assert loop._global_variables["timezone"] == "UTC"


def test_loop_factory_seeds_session_variables_from_persisted_values(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {"tokens_used": 999})
    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 999, "turn_count": 0}


def test_build_plugin_set_merges_caller_supplied_global_variables(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path), global_variables={"deployment": "staging"})["main"]
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._global_variables["deployment"] == "staging"
    assert loop._global_variables["model"] == "test-model"


def test_build_plugin_set_wires_the_detected_shell_into_global_variables(tmp_path):
    plugin_set = build_plugin_set(make_config(tmp_path))["main"]
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._global_variables["shell"] == _detect_shell()


def test_build_plugin_set_builds_every_named_group_in_the_yaml(tmp_path):
    yaml_text = "main:\n  tools: [read_file]\nagent:\n  tools: [bash]\n"
    plugin_sets = build_plugin_set(make_config(tmp_path, yaml_text=yaml_text))
    assert set(plugin_sets.keys()) == {"main", "agent"}
    assert plugin_sets["main"].tool_classes[0] is ReadFileToolPlugin
    assert issubclass(plugin_sets["agent"].tool_classes[0], BashToolPlugin)


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


def test_build_tool_classes_wires_bash_with_yaml_timeout(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="bash", params={"timeout": 99})], config, ctx)
    assert len(classes) == 1
    assert issubclass(classes[0], BashToolPlugin)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._timeout == 99


def test_build_tool_classes_bash_defaults_timeout_to_60_when_omitted(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="bash", params={})], config, ctx)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._timeout == 60.0


def test_build_tool_classes_wires_simple_tools_unmodified(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="read_file"), PluginSpec(name="write_file"), PluginSpec(name="edit_file")],
        config, ctx,
    )
    assert classes == (ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin)


def test_build_tool_classes_wires_skills_tools_with_project_root(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="list_skills"), PluginSpec(name="load_skill")], config, ctx,
    )
    list_tool = classes[0](workspace_dir="/tmp/ws")
    load_tool = classes[1](workspace_dir="/tmp/ws")
    assert list_tool._project_root == "/tmp"
    assert load_tool._project_root == "/tmp"


def test_build_tool_classes_unknown_tool_raises_plugin_config_error(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown tool"):
        _build_tool_classes([PluginSpec(name="does_not_exist")], config, ctx)


def test_build_tool_classes_web_search_without_api_key_raises(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="TAVILY_API_KEY"):
        _build_tool_classes([PluginSpec(name="web_search")], config, ctx)


def test_build_tool_classes_web_search_with_api_key_wires_timeout(tmp_path):
    config = make_config(tmp_path, TAVILY_API_KEY="tv")
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="web_search", params={"timeout": 11})], config, ctx)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "tv"
    assert tool._timeout == 11


def test_build_tool_classes_web_fetch_without_api_key_raises(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="FIRECRAWL_API_KEY"):
        _build_tool_classes([PluginSpec(name="web_fetch")], config, ctx)


def test_build_tool_classes_web_fetch_with_api_key_and_no_summary_model(tmp_path):
    config = make_config(tmp_path, FIRECRAWL_API_KEY="fc")
    ctx = make_build_context(config)
    classes = _build_tool_classes([PluginSpec(name="web_fetch", params={"max_chars": 5000})], config, ctx)
    assert classes[0].schema == build_web_fetch_schema(include_prompt=False)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._api_key == "fc"
    assert tool._max_chars == 5000
    assert tool._summary_client is None


def test_build_tool_classes_web_fetch_summary_model_enables_prompt_and_shared_client(tmp_path):
    config = make_config(tmp_path, FIRECRAWL_API_KEY="fc")
    ctx = make_build_context(config)
    classes = _build_tool_classes(
        [PluginSpec(name="web_fetch", params={"summary_model": "fast-model"})], config, ctx,
    )
    assert classes[0].schema == build_web_fetch_schema(include_prompt=True)
    tool = classes[0](workspace_dir="/tmp/ws")
    assert tool._summary_model == "fast-model"
    assert tool._summary_client is ctx.shared_client


def test_build_sections_resolves_each_name_to_its_plugin_type(tmp_path):
    config = make_config(tmp_path)
    ctx = BuildContext(shared_client=make_build_context(config).shared_client, prompts={"identity": "hi"})
    sections = _build_sections(
        ["identity", "tooling", "skills", "workspace", "runtime", "execution"], config, ctx, "/tmp/ws", [],
    )
    kinds = [type(s) for s in sections]
    assert kinds == [
        IdentitySectionPlugin, ToolingSectionPlugin, SkillsSectionPlugin,
        WorkspaceSectionPlugin, RuntimeSectionPlugin, ExecutionBiasSectionPlugin,
    ]


def test_build_sections_unknown_name_raises(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown section"):
        _build_sections(["not_a_real_section"], config, ctx, "/tmp/ws", [])


def test_build_context_plugins_wires_configured_values(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    specs = [
        PluginSpec(name="turn_variables"),
        PluginSpec(name="system_prompt", params={"sections": ["identity"]}),
        PluginSpec(name="truncator", params={"keep_last_n": 9}),
        PluginSpec(name="token_budget", params={"budget_tokens": 123}),
        PluginSpec(name="extra_prompt", params={"sections": ["dynamic_state"]}),
    ]
    factories = _build_context_plugins(specs, config, ctx)
    instances = [factory("/tmp/ws", []) for factory in factories]
    kinds = [type(p) for p in instances]
    assert kinds == [
        TurnVariableUpdaterPlugin, SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin, ExtraPromptPlugin,
    ]
    assert instances[2]._keep_last_n == 9
    assert instances[3]._budget_tokens == 123


def test_build_context_plugins_produces_fresh_instances_each_call(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    factories = _build_context_plugins([PluginSpec(name="turn_variables")], config, ctx)
    assert factories[0]("/tmp/ws", []) is not factories[0]("/tmp/ws", [])


def test_build_context_plugins_unknown_name_raises(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown context plugin"):
        _build_context_plugins([PluginSpec(name="not_real")], config, ctx)


def test_build_policy_plugins_wires_configured_max_steps(tmp_path):
    config = make_config(tmp_path)
    specs = [PluginSpec(name="permission"), PluginSpec(name="step_limit", params={"max_steps": 7})]
    factories = _build_policy_plugins(specs, config)
    instances = [factory() for factory in factories]
    kinds = [type(p) for p in instances]
    assert kinds == [PermissionPolicyPlugin, StepLimitPlugin]
    assert instances[1]._max_steps == 7


def test_build_policy_plugins_produces_fresh_instances(tmp_path):
    config = make_config(tmp_path)
    factories = _build_policy_plugins([PluginSpec(name="permission")], config)
    assert factories[0]() is not factories[0]()


def test_build_policy_plugins_unknown_name_raises(tmp_path):
    config = make_config(tmp_path)
    with pytest.raises(PluginConfigError, match="unknown policy plugin"):
        _build_policy_plugins([PluginSpec(name="not_real")], config)


def test_build_summarizer_default_produces_summarizer_plugin():
    factory = _build_summarizer("default")
    assert isinstance(factory(), SummarizerPlugin)
    assert factory() is not factory()


def test_build_summarizer_unknown_name_raises():
    with pytest.raises(PluginConfigError, match="unknown summarizer"):
        _build_summarizer("not_real")


def test_build_backend_openrouter_wires_model_and_blacklist(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    backend = _build_backend("openrouter", config, ctx, ["novita"])
    instance = backend("discord:1")
    assert isinstance(instance, OpenRouterModelPlugin)
    assert instance.model == "test-model"
    assert instance._provider_blacklist == ["novita"]
    assert instance._client is ctx.shared_client


def test_build_backend_unknown_name_raises(tmp_path):
    config = make_config(tmp_path)
    ctx = make_build_context(config)
    with pytest.raises(PluginConfigError, match="unknown backend"):
        _build_backend("not_real", config, ctx, [])


def test_build_plugin_set_end_to_end_matches_yaml(tmp_path):
    config = make_config(tmp_path)
    plugin_set = build_plugin_set(config)["main"]

    assert len(plugin_set.tool_classes) == 6
    bash_tool = plugin_set.tool_classes[0](workspace_dir="/tmp/ws")
    assert bash_tool._timeout == 42

    instances = [factory("/tmp/ws", []) for factory in plugin_set.context_plugins]
    assert [type(p) for p in instances] == [
        TurnVariableUpdaterPlugin, SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin, ExtraPromptPlugin,
    ]
    assert instances[2]._keep_last_n == 9
    assert instances[3]._budget_tokens == 123

    policy_instances = [factory() for factory in plugin_set.policy_plugins]
    assert [type(p) for p in policy_instances] == [PermissionPolicyPlugin, StepLimitPlugin]
    assert policy_instances[1]._max_steps == 7

    assert isinstance(plugin_set.summarizer(), SummarizerPlugin)
    assert isinstance(plugin_set.backend("discord:1"), OpenRouterModelPlugin)


def test_build_plugin_set_unknown_tool_in_yaml_raises(tmp_path):
    with pytest.raises(PluginConfigError, match="unknown tool"):
        build_plugin_set(make_config(tmp_path, yaml_text="main:\n  tools: [not_a_real_tool]\n"))


def test_build_plugin_set_web_search_without_key_raises(tmp_path):
    with pytest.raises(PluginConfigError, match="TAVILY_API_KEY"):
        build_plugin_set(make_config(tmp_path, yaml_text="main:\n  tools: [web_search]\n"))


def test_build_plugin_set_instantiate_session_registers_every_plugin_and_returns_loop(tmp_path):
    from conic.core.bus import MessageBus
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    class FakeHandle:
        def load_history(self):
            return []

    config = make_config(tmp_path)
    plugin_set = build_plugin_set(config)["main"]
    bus = MessageBus()

    loop_plugin = plugin_set.instantiate_session(bus, "/tmp/ws", "discord:1", FakeHandle(), {})

    assert isinstance(loop_plugin, ReactLoopPlugin)
    assert loop_plugin._session_variables["workspace_dir"] == "/tmp/ws"
