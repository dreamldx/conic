from conic.config import Config
from conic.core.manager import PluginSet
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.loops.react_loop import ReactLoopPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin

DEFAULT_SYSTEM_PROMPT = (
    "You are Conic, a helpful coding agent with access to bash, read_file, "
    "write_file, and edit_file tools scoped to this session's workspace directory."
)


def build_plugin_set(config: Config) -> PluginSet:
    return PluginSet(
        tool_classes=(BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin),
        backend=OpenRouterBackendPlugin(api_key=config.openrouter_api_key, model=config.openrouter_model),
        context_plugins=(
            SystemPromptPlugin(DEFAULT_SYSTEM_PROMPT),
            TruncatorPlugin(keep_last_n=config.truncate_keep_last_n),
            TokenBudgetPlugin(budget_tokens=config.context_token_budget),
        ),
        policy_plugins=(
            PermissionPolicyPlugin(),
            StepLimitPlugin(max_steps=config.max_steps_per_turn),
        ),
        summarizer=SummarizerPlugin(),
        loop_factory=lambda handle, schemas, payload_map: ReactLoopPlugin(handle, schemas, payload_map),
    )
