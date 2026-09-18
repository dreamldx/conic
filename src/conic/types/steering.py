from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar


class SteeringItem(ABC):
    source: str  # "user" | "background" | "system"

    @abstractmethod
    def to_history_entries(self) -> list[dict]: ...

    def is_turn_abort(self) -> bool:
        return False


@dataclass
class SteeringUserMessage(SteeringItem):
    text: str

    source: ClassVar[str] = "user"

    def to_history_entries(self) -> list[dict]:
        return [{"role": "user", "content": self.text}]


@dataclass
class SteeringBackgroundResult(SteeringItem):
    task_id: str
    exit_code: int | None
    output: str

    source: ClassVar[str] = "background"

    def to_history_entries(self) -> list[dict]:
        content = f"[background task {self.task_id}] exit_code={self.exit_code}\n{self.output}"
        return [{"role": "user", "content": content}]


@dataclass
class SteeringStopCommand(SteeringItem):
    source: ClassVar[str] = "system"

    def to_history_entries(self) -> list[dict]:
        return []

    def is_turn_abort(self) -> bool:
        return True
