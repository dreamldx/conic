from collections.abc import Callable

from openai import AsyncOpenAI

from conic.plugins.models.base import (
    BLOCKING_TIMEOUT,
    STREAM_ATTEMPT_TIMEOUTS,
    ChatCompletionModelPlugin,
)
from conic.services.models import ModelCatalogEntry

NANO_GPT_BASE_URL = "https://api.nano-gpt.com/api/v1"


class NanoGptModelPlugin(ChatCompletionModelPlugin):
    Name = "nano_gpt"

    def __init__(
        self,
        api_key: str,
        model: str,
        client: AsyncOpenAI | None = None,
        session_id: str | None = None,
        catalog_lookup: Callable[[str], list[ModelCatalogEntry]] | None = None,
        global_variables: dict | None = None,
        stream_timeouts: tuple[float, ...] = STREAM_ATTEMPT_TIMEOUTS,
        blocking_timeout: float = BLOCKING_TIMEOUT,
    ):
        super().__init__(
            model=model,
            client=client or AsyncOpenAI(base_url=NANO_GPT_BASE_URL, api_key=api_key),
            session_id=session_id,
            catalog_lookup=catalog_lookup,
            global_variables=global_variables,
            stream_timeouts=stream_timeouts,
            blocking_timeout=blocking_timeout,
        )

    def _extra_headers(self, idempotency_key: str) -> dict:
        return {"Idempotency-Key": idempotency_key}
