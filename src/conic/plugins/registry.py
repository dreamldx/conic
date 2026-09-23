import os
import platform
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath

from openai import AsyncOpenAI

from conic.config import Config
from conic.core.bus import infer_payload_type
from conic.core.manager import PluginSet
from conic.plugins.context.extra_prompt import ExtraPromptPlugin
from conic.plugins.context.sections.dynamic_state import DynamicStateSectionPlugin
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
from conic.plugins.loops.react_loop import ReactLoopPlugin
from conic.plugins.models.openrouter import OpenRouterModelPlugin
from conic.plugins.plugin_config import (
    PluginConfigError,
    PluginSetConfig,
    PluginSpec,
    load_plugins_config,
)
from conic.plugins.policy.permission import PermissionPolicyPlugin
from conic.plugins.policy.step_limit import StepLimitPlugin
from conic.plugins.tools.bash import BashToolPlugin
from conic.plugins.tools.edit_file import EditFileToolPlugin
from conic.plugins.tools.read_file import ReadFileToolPlugin
from conic.plugins.tools.skills import ListSkillsToolPlugin, LoadSkillToolPlugin
from conic.plugins.tools.web_fetch import WebFetchToolPlugin, build_web_fetch_schema
from conic.plugins.tools.web_search import WebSearchToolPlugin
from conic.plugins.tools.write_file import WriteFileToolPlugin


def _load_prompts(prompts_dir: Path) -> dict[str, str]:
    prompts: dict[str, str] = {}
    if prompts_dir.is_dir():
        for md_file in prompts_dir.glob("*.md"):
            prompts[md_file.stem] = md_file.read_text(encoding="utf-8").strip()
    return prompts


def _detect_shell() -> str:
    # Matches what asyncio.create_subprocess_shell (used by BashToolPlugin) actually
    # invokes under shell=True: %ComSpec% on Windows, /bin/sh on POSIX -- not the
    # interactive shell conic's own process happens to be running under. On macOS
    # /bin/sh is bash in sh mode (via /var/select/sh), so report the real thing.
    if platform.system() == "Windows":
        # PureWindowsPath (not Path) because this parses a Windows-style
        # backslash path per Windows rules regardless of the host OS --
        # plain Path resolves to PosixPath on a POSIX host and would treat
        # the whole backslash string as one component, breaking .name.
        return PureWindowsPath(os.environ.get("ComSpec", r"C:\Windows\System32\cmd.exe")).name
    if platform.system() == "Darwin":
        return "/bin/bash"
    return "/bin/sh"


def build_plugin_set(config: Config, global_variables: dict | None = None) -> dict[str, PluginSet]:
    prompts = _load_prompts(Path(config.project_root) / "prompts")
    shared_client = AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=config.openrouter_api_key)
    ctx = BuildContext(shared_client=shared_client, prompts=prompts)

    resolved_global_variables = {
        "model": config.openrouter_model,
        "platform": f"{platform.system()} {platform.release()}",
        "shell": _detect_shell(),
        "timezone": "UTC",
        **(global_variables or {}),
    }
    provider_blacklist = [p.strip() for p in config.openrouter_provider_blacklist.split(",") if p.strip()]

    plugins_cfg = load_plugins_config(config.plugins_config_path)

    return {
        name: _build_one_plugin_set(set_cfg, config, ctx, resolved_global_variables, provider_blacklist)
        for name, set_cfg in plugins_cfg.items()
    }


def _build_one_plugin_set(
    set_cfg: PluginSetConfig,
    config: Config,
    ctx: "BuildContext",
    resolved_global_variables: dict,
    provider_blacklist: list[str],
) -> PluginSet:
    tool_classes = _build_tool_classes(set_cfg.tools, config, ctx)
    context_plugins = _build_context_plugins(set_cfg.context, config, ctx)
    policy_plugins = _build_policy_plugins(set_cfg.policy, config)
    summarizer = _build_summarizer(set_cfg.summarizer)
    backend = _build_backend(set_cfg.backend, config, ctx, provider_blacklist)
    loop_cls = _build_loop(set_cfg.loop)

    def loop_factory(handle, schemas, payload_map, ws, persisted_session_variables):
        return loop_cls(
            handle, schemas, payload_map,
            workspace_dir=ws,
            global_variables=resolved_global_variables,
            persisted_session_variables=persisted_session_variables,
        )

    def instantiate_session(bus, workspace_dir, session_key, handle, persisted_session_variables):
        tool_schemas = [cls.schema for cls in tool_classes]
        tool_payload_map = {cls.llm_name: infer_payload_type(cls.execute) for cls in tool_classes}

        for tool_cls in tool_classes:
            tool_cls(workspace_dir=workspace_dir).register(bus)

        backend(session_key).register(bus)
        for ctx_factory in context_plugins:
            ctx_factory(workspace_dir, tool_schemas).register(bus)
        for policy_factory in policy_plugins:
            policy_factory().register(bus)
        summarizer().register(bus)

        loop_plugin = loop_factory(handle, tool_schemas, tool_payload_map, workspace_dir, persisted_session_variables)
        loop_plugin.register(bus)
        return loop_plugin

    return PluginSet(
        tool_classes=tool_classes,
        backend=backend,
        context_plugins=context_plugins,
        policy_plugins=policy_plugins,
        summarizer=summarizer,
        loop_factory=loop_factory,
        instantiate_session=instantiate_session,
    )


@dataclass
class BuildContext:
    shared_client: AsyncOpenAI
    prompts: dict[str, str]


def _build_bash_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    timeout = params.get("timeout", 60.0)

    class ConfiguredBashToolPlugin(BashToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, timeout=timeout)

    return ConfiguredBashToolPlugin


def _build_list_skills_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    class ConfiguredListSkillsToolPlugin(ListSkillsToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    return ConfiguredListSkillsToolPlugin


def _build_load_skill_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    class ConfiguredLoadSkillToolPlugin(LoadSkillToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, project_root=config.project_root)

    return ConfiguredLoadSkillToolPlugin


def _build_web_search_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    if not config.tavily_api_key:
        raise PluginConfigError("web_search requires TAVILY_API_KEY to be set")
    timeout = params.get("timeout", 30.0)

    class ConfiguredWebSearchToolPlugin(WebSearchToolPlugin):
        def __init__(self, workspace_dir: str):
            super().__init__(workspace_dir=workspace_dir, api_key=config.tavily_api_key, timeout=timeout)

    return ConfiguredWebSearchToolPlugin


def _build_web_fetch_tool(config: Config, params: dict, ctx: BuildContext) -> type:
    if not config.firecrawl_api_key:
        raise PluginConfigError("web_fetch requires FIRECRAWL_API_KEY to be set")
    timeout = params.get("timeout", 60.0)
    max_chars = params.get("max_chars", 15000)
    summary_model = params.get("summary_model", "")

    class ConfiguredWebFetchToolPlugin(WebFetchToolPlugin):
        schema = build_web_fetch_schema(include_prompt=bool(summary_model))

        def __init__(self, workspace_dir: str):
            super().__init__(
                workspace_dir=workspace_dir, api_key=config.firecrawl_api_key,
                timeout=timeout, max_chars=max_chars, summary_model=summary_model,
                summary_client=ctx.shared_client if summary_model else None,
            )

    return ConfiguredWebFetchToolPlugin


TOOL_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], type]] = {
    "bash": _build_bash_tool,
    "read_file": lambda config, params, ctx: ReadFileToolPlugin,
    "write_file": lambda config, params, ctx: WriteFileToolPlugin,
    "edit_file": lambda config, params, ctx: EditFileToolPlugin,
    "list_skills": _build_list_skills_tool,
    "load_skill": _build_load_skill_tool,
    "web_search": _build_web_search_tool,
    "web_fetch": _build_web_fetch_tool,
}


def _build_tool_classes(specs: list[PluginSpec], config: Config, ctx: BuildContext) -> tuple[type, ...]:
    classes = []
    for spec in specs:
        builder = TOOL_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown tool: {spec.name}")
        classes.append(builder(config, spec.params, ctx))
    return tuple(classes)


SectionBuilder = Callable[[Config, dict, BuildContext, str, list[dict]], object]

SECTION_BUILDERS: dict[str, SectionBuilder] = {
    "identity": lambda config, params, ctx, ws, schemas: IdentitySectionPlugin(ctx.prompts.get("identity", "")),
    "tooling": lambda config, params, ctx, ws, schemas: ToolingSectionPlugin(schemas),
    "skills": lambda config, params, ctx, ws, schemas: SkillsSectionPlugin(ws, config.project_root),
    "workspace": lambda config, params, ctx, ws, schemas: WorkspaceSectionPlugin(ws),
    "runtime": lambda config, params, ctx, ws, schemas: RuntimeSectionPlugin(),
    "execution": lambda config, params, ctx, ws, schemas: ExecutionBiasSectionPlugin(ctx.prompts.get("execution", "")),
    "dynamic_state": lambda config, params, ctx, ws, schemas: DynamicStateSectionPlugin(),
}


def _build_sections(
    names: list[str], config: Config, ctx: BuildContext, ws: str, schemas: list[dict]
) -> list:
    sections = []
    for name in names:
        builder = SECTION_BUILDERS.get(name)
        if builder is None:
            raise PluginConfigError(f"unknown section: {name}")
        sections.append(builder(config, {}, ctx, ws, schemas))
    return sections


def _build_turn_variables_context(config: Config, params: dict, ctx: BuildContext):
    return lambda ws, schemas: TurnVariableUpdaterPlugin()


def _build_system_prompt_context(config: Config, params: dict, ctx: BuildContext):
    section_names = params.get("sections", [])
    return lambda ws, schemas: SystemPromptPlugin(_build_sections(section_names, config, ctx, ws, schemas))


def _build_truncator_context(config: Config, params: dict, ctx: BuildContext):
    keep_last_n = params.get("keep_last_n", 40)
    return lambda ws, schemas: TruncatorPlugin(keep_last_n=keep_last_n)


def _build_token_budget_context(config: Config, params: dict, ctx: BuildContext):
    budget_tokens = params.get("budget_tokens", 50000)
    return lambda ws, schemas: TokenBudgetPlugin(budget_tokens=budget_tokens)


def _build_extra_prompt_context(config: Config, params: dict, ctx: BuildContext):
    section_names = params.get("sections", [])
    return lambda ws, schemas: ExtraPromptPlugin(_build_sections(section_names, config, ctx, ws, schemas))


CONTEXT_BUILDERS: dict[str, Callable[[Config, dict, BuildContext], Callable[[str, list[dict]], object]]] = {
    "turn_variables": _build_turn_variables_context,
    "system_prompt": _build_system_prompt_context,
    "truncator": _build_truncator_context,
    "token_budget": _build_token_budget_context,
    "extra_prompt": _build_extra_prompt_context,
}


def _build_context_plugins(
    specs: list[PluginSpec], config: Config, ctx: BuildContext
) -> tuple[Callable[[str, list[dict]], object], ...]:
    factories = []
    for spec in specs:
        builder = CONTEXT_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown context plugin: {spec.name}")
        factories.append(builder(config, spec.params, ctx))
    return tuple(factories)


def _build_permission_policy(config: Config, params: dict):
    return lambda: PermissionPolicyPlugin()


def _build_step_limit_policy(config: Config, params: dict):
    max_steps = params.get("max_steps", 25)
    return lambda: StepLimitPlugin(max_steps=max_steps)


POLICY_BUILDERS: dict[str, Callable[[Config, dict], Callable[[], object]]] = {
    "permission": _build_permission_policy,
    "step_limit": _build_step_limit_policy,
}


def _build_policy_plugins(specs: list[PluginSpec], config: Config) -> tuple[Callable[[], object], ...]:
    factories = []
    for spec in specs:
        builder = POLICY_BUILDERS.get(spec.name)
        if builder is None:
            raise PluginConfigError(f"unknown policy plugin: {spec.name}")
        factories.append(builder(config, spec.params))
    return tuple(factories)


SUMMARIZER_BUILDERS: dict[str, Callable[[], Callable[[], object]]] = {
    "default": lambda: (lambda: SummarizerPlugin()),
}


def _build_summarizer(name: str) -> Callable[[], object]:
    builder = SUMMARIZER_BUILDERS.get(name)
    if builder is None:
        raise PluginConfigError(f"unknown summarizer: {name}")
    return builder()


def _build_openrouter_backend(config: Config, ctx: BuildContext, provider_blacklist: list[str]):
    def factory(session_key: str):
        return OpenRouterModelPlugin(
            api_key=config.openrouter_api_key, model=config.openrouter_model, client=ctx.shared_client,
            provider_blacklist=provider_blacklist, session_id=session_key, app_name=config.project_name,
        )
    return factory


BACKEND_BUILDERS: dict[str, Callable[[Config, BuildContext, list[str]], Callable[[str], object]]] = {
    "openrouter": _build_openrouter_backend,
}


def _build_backend(
    name: str, config: Config, ctx: BuildContext, provider_blacklist: list[str]
) -> Callable[[str], object]:
    builder = BACKEND_BUILDERS.get(name)
    if builder is None:
        raise PluginConfigError(f"unknown backend: {name}")
    return builder(config, ctx, provider_blacklist)


LOOP_BUILDERS: dict[str, Callable[[], type]] = {
    "react": lambda: ReactLoopPlugin,
}


def _build_loop(name: str) -> type:
    builder = LOOP_BUILDERS.get(name)
    if builder is None:
        raise PluginConfigError(f"unknown loop: {name}")
    return builder()
