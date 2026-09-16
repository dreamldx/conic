from typing import Protocol


class Gateway(Protocol):
    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...
