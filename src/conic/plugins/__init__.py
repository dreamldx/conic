from __future__ import annotations

from typing import ClassVar


class Plugin:
    _registry: ClassVar[dict[str, type[Plugin]]] = {}
    Name: ClassVar[str]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if "Name" not in cls.__dict__:
            raise TypeError(f"Plugin subclass {cls.__name__} must define 'Name'")
        if cls.Name not in Plugin._registry:
            Plugin._registry[cls.Name] = cls

    @classmethod
    def find(cls, name: str) -> type[Plugin] | None:
        return cls._registry.get(name)

    def register(self, bus) -> None:
        raise NotImplementedError