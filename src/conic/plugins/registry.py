from conic.config import Config
from conic.core.manager import PluginSet
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
from conic.plugins.context.sections.tooling import ToolingSectionPlugin
from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
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


def build_plugin_set(config: Config) -> PluginSet:
    return PluginSet(
        tool_classes=(BashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin),
        backend=OpenRouterBackendPlugin(api_key=config.openrouter_api_key, model=config.openrouter_model),
        context_plugins=(
            lambda ws, schemas: SystemPromptPlugin([
                IdentitySectionPlugin(),
                ToolingSectionPlugin(schemas),
                WorkspaceSectionPlugin(ws),
                RuntimeSectionPlugin(config.openrouter_model),
                ExecutionBiasSectionPlugin(),
            ]),
            lambda ws, schemas: TruncatorPlugin(keep_last_n=config.truncate_keep_last_n),
            lambda ws, schemas: TokenBudgetPlugin(budget_tokens=config.context_token_budget),
        ),
        policy_plugins=(
            lambda: PermissionPolicyPlugin(),
            lambda: StepLimitPlugin(max_steps=config.max_steps_per_turn),
        ),
        summarizer=lambda: SummarizerPlugin(),
        loop_factory=lambda handle, schemas, payload_map: ReactLoopPlugin(handle, schemas, payload_map),
    )
