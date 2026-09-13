import asyncio
from dataclasses import dataclass, field

from conic.core.bus import MessageBus
from conic.services.storage import SessionRow


@dataclass
class SessionScope:
    bus: MessageBus
    row: SessionRow
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
