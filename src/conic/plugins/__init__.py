from typing import ClassVar


class Plugin:
    Name: ClassVar[str]

    def __init_subclass__(cls, **kwargs) -> None:
        super().__init_subclass__(**kwargs)
        if "Name" not in cls.__dict__:
            raise TypeError(f"Plugin subclass {cls.__name__} must define 'Name'")

    def register(self, bus) -> None:
        raise NotImplementedError