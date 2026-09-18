from enum import Enum


class AbortReason(Enum):
    POLICY = "policy"
    MODEL_TIMEOUT = "model_timeout"
    USER_ABORT = "user_abort"

    @property
    def ends_session(self) -> bool:
        return self is AbortReason.USER_ABORT


class AbortTurn(Exception):
    """Raised by any hook to abort the current turn cleanly."""

    def __init__(self, message: str = "", reason: AbortReason = AbortReason.POLICY):
        super().__init__(message)
        self.reason = reason


class NoResponderError(Exception):
    """Raised by MessageBus.request when no handler matches the payload type."""


class DuplicateResponderError(Exception):
    """Raised by MessageBus.on_request when a second responder is registered
    for the same (topic, payload type) pair."""


class DuplicateMailboxError(Exception):
    """Raised by MessageBus.create_mailbox when a second consumer is registered
    for the same mailbox name."""


class UnknownMailboxError(Exception):
    """Raised by MessageBus.post/drain/wait_multiply_mailbox when the mailbox
    name has no consumer registered via create_mailbox."""
