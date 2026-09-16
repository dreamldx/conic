import platform
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

from conic.config import Config
from conic.core.manager import PluginSet
from conic.plugins.backends.openrouter import OpenRouterBackendPlugin
from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
from conic.plugins.context.sections.extra import ExtraPromptPlugin
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.sections.tooling import ToolingSectionPlugin
from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.plugins.loops.react_loop import ReactLoopPlugin
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def _load_prompts(prompts_dir: Path) -> dict[str, str]:
    prompts: dict[str, str] = {}
    if prompts_dir.is_dir():
        for md_file in prompts_dir.glob("*.md"):
            prompts[md_file.stem] = md_file.read_text(encoding="utf-8").strip()
    return prompts


def build_plugin_set(config: Config, global_variables: dict | None = None) -> PluginSet:
    prompts = _load_prompts(Path(config.project_root) / "prompts")
    shared_client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)

    resolved_global_variables = {
        "model": config.openrouter_model,
        "platform": f"{platform.system()} {platform.release()}",
        "timezone": str(datetime.now().astimezone().tzinfo),
        **(global_variables or {}),
    }

    class ConfiguredBashToolPlugin(BashToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, timeout=config.bash_timeout)

    return PluginSet(
        tool_classes=(ConfiguredBashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin),
        backend=lambda: OpenRouterBackendPlugin(
            api_key=config.openrouter_api_key, model=config.openrouter_model, client=shared_client,
        ),
        context_plugins=(
            lambda ws, schemas: TurnVariableUpdaterPlugin(),
            lambda ws, schemas: SystemPromptPlugin([
                IdentitySectionPlugin(prompts.get("identity", "")),
                ToolingSectionPlugin(schemas),
                WorkspaceSectionPlugin(ws),
                ExtraPromptPlugin(),
                ExecutionBiasSectionPlugin(prompts.get("execution", "")),
            ]),
            lambda ws, schemas: TruncatorPlugin(keep_last_n=config.truncate_keep_last_n),
            lambda ws, schemas: TokenBudgetPlugin(budget_tokens=config.context_token_budget),
        ),
        policy_plugins=(
            lambda: PermissionPolicyPlugin(),
            lambda: StepLimitPlugin(max_steps=config.max_steps_per_turn),
        ),
        summarizer=lambda: SummarizerPlugin(),
        loop_factory=lambda handle, schemas, payload_map, ws, persisted_session_variables: ReactLoopPlugin(
            handle, schemas, payload_map,
            workspace_dir=ws,
            global_variables=resolved_global_variables,
            persisted_session_variables=persisted_session_variables,
        ),
    )
