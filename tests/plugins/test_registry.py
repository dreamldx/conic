from conic.config import Config
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.registry import build_plugin_set
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def make_config():
    return Config(
        PROJECT_ROOT="/tmp",
        DISCORD_BOT_TOKEN="d", OPENROUTER_API_KEY="k", OPENROUTER_MODEL="test-model",
        WORKSPACE_ROOT="./workspace", DUCKDB_PATH="./data/conic.duckdb",
        LOG_LEVEL="DEBUG", MAX_STEPS_PER_TURN=7, CONTEXT_TOKEN_BUDGET=123, TRUNCATE_KEEP_LAST_N=9,
        BASH_TIMEOUT=42,
    )


def test_build_plugin_set_wires_the_four_v1_tools():
    plugin_set = build_plugin_set(make_config())
    assert issubclass(plugin_set.tool_classes[0], BashToolPlugin)
    assert plugin_set.tool_classes[1:] == (ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin)


def test_build_plugin_set_wires_bash_tool_with_configured_timeout():
    plugin_set = build_plugin_set(make_config())
    bash_cls = plugin_set.tool_classes[0]
    assert bash_cls.llm_name == "bash"
    assert bash_cls.schema == BashToolPlugin.schema

    bash_tool = bash_cls(workspace_dir="/tmp/ws")
    assert bash_tool._timeout == 42


def test_build_plugin_set_wires_backend_with_configured_model():
    plugin_set = build_plugin_set(make_config())
    backend = plugin_set.backend()
    assert isinstance(backend, OpenRouterBackendPlugin)
    assert backend.model == "test-model"


def test_build_plugin_set_backend_factory_produces_fresh_instances_sharing_one_client():
    plugin_set = build_plugin_set(make_config())
    backend1 = plugin_set.backend()
    backend2 = plugin_set.backend()
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
    assert "timezone" in loop._global_variables


def test_loop_factory_seeds_session_variables_from_persisted_values():
    plugin_set = build_plugin_set(make_config())
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {"tokens_used": 999})
    assert loop._session_variables == {"workspace_dir": "/tmp/ws", "tokens_used": 999, "turn_count": 0}


def test_build_plugin_set_merges_caller_supplied_global_variables():
    plugin_set = build_plugin_set(make_config(), global_variables={"deployment": "staging"})
    loop = plugin_set.loop_factory(object(), [], {}, "/tmp/ws", {})
    assert loop._global_variables["deployment"] == "staging"
    assert loop._global_variables["model"] == "test-model"
