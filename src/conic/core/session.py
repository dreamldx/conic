from dataclasses import dataclass

from conic.core.bus import MessageBus
from conic.services.storage import SessionRow


@dataclass
class SessionScope:
    bus: MessageBus
    row: SessionRow
