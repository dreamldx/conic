class AbortTurn(Exception):
    """Raised by any hook to abort the current turn cleanly."""


class NoResponderError(Exception):
    """Raised by MessageBus.request when no handler matches the payload type."""


class DuplicateResponderError(Exception):
    """Raised by MessageBus.on_request when a second responder is registered
    for the same (topic, payload type) pair."""
