from datetime import datetime

from pydantic import BaseModel, Field


class Session(BaseModel):
    session_key: str
    channel: str
    native_id: str
    workspace_dir: str
    model: str
    status: str
    created_at: datetime


class Message(BaseModel):
    session_key: str = ""
    seq: int = 0
    role: str = ""
    content: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now())
