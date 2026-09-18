import asyncio
from dataclasses import dataclass, field

from conic.core.bus import MessageBus
from conic.services.models import Session


@dataclass
class SessionScope:
    bus: MessageBus
    row: Session
    closing: bool = False
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    tasks: dict[str, asyncio.Task] = field(default_factory=dict)
