import math
from datetime import UTC, datetime

from pydantic import BaseModel, Field


def safe_float(value, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


class Session(BaseModel):
    session_key: str
    channel: str
    native_id: str
    workspace_dir: str
    model: str
    status: str
    created_at: datetime
    variables: dict = Field(default_factory=dict)


class Message(BaseModel):
    session_key: str = ""
    seq: int = 0
    role: str = ""
    content: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    turn_id: int = 0
    duration_ms: int | None = None


class ModelCatalogEntry(BaseModel):
    slug: str
    provider: str = "openrouter"
    vendor: str = ""
    real_model: str = ""
    name: str = ""
    description: str = ""
    context_length: int = 0
    supports_tools: bool = False
    pricing_prompt: float = 0.0
    pricing_completion: float = 0.0
    input_modalities: list[str] = Field(default_factory=list)
    output_modalities: list[str] = Field(default_factory=list)
    supported_parameters: list[str] = Field(default_factory=list)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
