import os
import platform
from datetime import datetime
from pathlib import Path

from openai import AsyncOpenAI

from conic.config import Config
from conic.core.manager import PluginSet
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.sections.dynamic_state import DynamicStateSectionPlugin
from conic.plugins.context.sections.execution import ExecutionBiasSectionPlugin
from conic.plugins.context.sections.identity import IdentitySectionPlugin
from conic.plugins.context.sections.runtime import RuntimeSectionPlugin
from conic.plugins.context.sections.tooling import ToolingSectionPlugin
from conic.plugins.context.sections.workspace import WorkspaceSectionPlugin
from conic.plugins.context.summarizer import SummarizerPlugin
from conic.plugins.context.system_prompt import SystemPromptPlugin
from conic.plugins.context.token_budget import TokenBudgetPlugin
from conic.plugins.context.truncator import TruncatorPlugin
from conic.plugins.context.variables import TurnVariableUpdaterPlugin
from conic.plugins.loops.react_loop import ReactLoopPlugin
from conic.plugins.models.openrouter import OpenRouterModelPlugin
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


def _detect_shell() -> str:
    # Matches what asyncio.create_subprocess_shell (used by BashToolPlugin) actually
    # invokes under shell=True: %ComSpec% on Windows, always /bin/sh on POSIX --
    # not the interactive shell conic's own process happens to be running under.
    if platform.system() == "Windows":
        return Path(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")).name
    return "/bin/sh"


def build_plugin_set(config: Config, global_variables: dict | None = None) -> PluginSet:
    prompts = _load_prompts(Path(config.project_root) / "prompts")
    shared_client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)

    resolved_global_variables = {
        "model": config.openrouter_model,
        "platform": f"{platform.system()} {platform.release()}",
        "shell": _detect_shell(),
        "timezone": str(datetime.now().astimezone().tzinfo),
        **(global_variables or {}),
    }
    provider_blacklist = [p.strip() for p in config.openrouter_provider_blacklist.split(",") if p.strip()]

    class ConfiguredBashToolPlugin(BashToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, timeout=config.bash_timeout)

    return PluginSet(
        tool_classes=(ConfiguredBashToolPlugin, ReadFileToolPlugin, WriteFileToolPlugin, EditFileToolPlugin),
        backend=lambda session_key: OpenRouterModelPlugin(
            api_key=config.openrouter_api_key, model=config.openrouter_model, client=shared_client,
            provider_blacklist=provider_blacklist,
            session_id=session_key, app_name=config.project_name,
        ),
        context_plugins=(
            lambda ws, schemas: TurnVariableUpdaterPlugin(),
            lambda ws, schemas: SystemPromptPlugin([
                IdentitySectionPlugin(prompts.get("identity", "")),
                ToolingSectionPlugin(schemas),
                WorkspaceSectionPlugin(ws),
                RuntimeSectionPlugin(),
                ExecutionBiasSectionPlugin(prompts.get("execution", "")),
            ]),
            lambda ws, schemas: TruncatorPlugin(keep_last_n=config.truncate_keep_last_n),
            lambda ws, schemas: TokenBudgetPlugin(budget_tokens=config.context_token_budget),
            lambda ws, schemas: ExtraPromptPlugin([
                DynamicStateSectionPlugin()
            ]),
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
