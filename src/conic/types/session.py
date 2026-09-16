import asyncio
from dataclasses import dataclass, field

from conic.core.bus import MessageBus
from conic.services.models import Session


@dataclass
class SessionScope:
    bus: MessageBus
    row: Session
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
