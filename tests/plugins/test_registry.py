from conic.config import Config
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.registry import build_plugin_set
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def make_config():
    return Config(
        discord_bot_token="d", openrouter_api_key="k", openrouter_model="test-model",
        workspace_root="./workspace", duckdb_path="./data/conic.duckdb",
        max_steps_per_turn=7, context_token_budget=123, truncate_keep_last_n=9,
    )


def test_build_plugin_set_wires_the_four_v1_tools():
    plugin_set = build_plugin_set(make_config())
    assert plugin_set.tool_classes == (
        BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin,
    )


def test_build_plugin_set_wires_backend_with_configured_model():
    plugin_set = build_plugin_set(make_config())
    assert isinstance(plugin_set.backend, OpenRouterBackendPlugin)
    assert plugin_set.backend.model == "test-model"


def test_build_plugin_set_wires_context_chain_with_configured_values():
    plugin_set = build_plugin_set(make_config())
    kinds = [type(p) for p in plugin_set.context_plugins]
    assert kinds == [SystemPromptPlugin, TruncatorPlugin, TokenBudgetPlugin]
    truncator = plugin_set.context_plugins[1]
    assert truncator._keep_last_n == 9
    token_budget = plugin_set.context_plugins[2]
    assert token_budget._budget_tokens == 123


def test_build_plugin_set_wires_policy_plugins_with_configured_max_steps():
    plugin_set = build_plugin_set(make_config())
    kinds = [type(p) for p in plugin_set.policy_plugins]
    assert kinds == [PermissionPolicyPlugin, StepLimitPlugin]
    step_limit = plugin_set.policy_plugins[1]
    assert step_limit._max_steps == 7


def test_build_plugin_set_wires_summarizer():
    plugin_set = build_plugin_set(make_config())
    assert isinstance(plugin_set.summarizer, SummarizerPlugin)


def test_loop_factory_produces_a_react_loop_plugin():
    from conic.plugins.loops.react_loop import ReactLoopPlugin

    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {})
    assert isinstance(loop, ReactLoopPlugin)
