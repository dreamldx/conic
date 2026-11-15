from collections.abc import Callable

from loguru import logger
from openai import AsyncOpenAI

from conic.plugins import meta
from conic.plugins.models.base import (
    BLOCKING_TIMEOUT,
    STREAM_ATTEMPT_TIMEOUTS,
    ChatCompletionModelPlugin,
)
from conic.services.models import ModelCatalogEntry
from conic.types.messages import (
    BuildSystemPrompt,
    SwitchModelRequest,
    SwitchModelResult,
)

APP_HTTP_REFERER = "https://github.com/dreamldx/conic"
MODEL_CATALOG_PATH = "conic/data/openrouter_models.json"
MODEL_CATALOG_SECTION = (
    "OpenRouter model information (slug, vendor, real_model, name, description, "
    "context length, pricing, modalities, supported parameters) is stored as JSON at "
    f"{MODEL_CATALOG_PATH}."
)

__all__ = [
    "APP_HTTP_REFERER",
    "BLOCKING_TIMEOUT",
    "MODEL_CATALOG_PATH",
    "MODEL_CATALOG_SECTION",
    "STREAM_ATTEMPT_TIMEOUTS",
    "OpenRouterModelPlugin",
]


class OpenRouterModelPlugin(ChatCompletionModelPlugin):
    Name = "openrouter"

    def __init__(
        self,
        api_key: str,
        model: str,
        client: AsyncOpenAI | None = None,
        provider_blacklist: list[str] | None = None,
        session_id: str | None = None,
        app_name: str | None = None,
        catalog_lookup: Callable[[str], list[ModelCatalogEntry]] | None = None,
        global_variables: dict | None = None,
        stream_timeouts: tuple[float, ...] = STREAM_ATTEMPT_TIMEOUTS,
        blocking_timeout: float = BLOCKING_TIMEOUT,
    ):
        self._provider_blacklist = provider_blacklist or []
        self._app_name = app_name
        super().__init__(
            model=model,
            client=client or AsyncOpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key),
            session_id=session_id,
            catalog_lookup=catalog_lookup,
            global_variables=global_variables,
            stream_timeouts=stream_timeouts,
            blocking_timeout=blocking_timeout,
        )

    def register(self, bus) -> None:
        super().register(bus)
        bus.on_request(meta.SwitchModelRequestEvent, self.switch_model)
        bus.on_chain(meta.BuildSystemPromptEvent, self.contribute_model_catalog_info)

    async def switch_model(self, msg: SwitchModelRequest) -> SwitchModelResult:
        entry, error = self._resolve_catalog_entry(msg.model_id)
        if error:
            return SwitchModelResult(error=error)
        real_model = entry.real_model or entry.slug
        logger.info("switching model {} -> {}", self.model, real_model)
        self.model = real_model
        self._reload_model_context_length(entry)
        return SwitchModelResult(model=real_model)

    async def contribute_model_catalog_info(self, msg: BuildSystemPrompt) -> BuildSystemPrompt:
        msg.sections["openrouter_models"] = MODEL_CATALOG_SECTION
        return msg

    def _extra_body(self) -> dict | None:
        if not self._provider_blacklist:
            return None
        return {"provider": {"ignore": self._provider_blacklist}}

    def _extra_headers(self, idempotency_key: str) -> dict:
        headers: dict = {"HTTP-Referer": APP_HTTP_REFERER}
        if self._session_id:
            headers["x-session-id"] = self._session_id
        if self._app_name:
            headers["X-OpenRouter-Title"] = self._app_name
        return headers
