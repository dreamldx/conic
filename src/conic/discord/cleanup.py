import asyncio
from collections.abc import Awaitable, Callable

from loguru import logger

DEFAULT_CLEANUP_INTERVAL_SECONDS = 3600


async def run_periodic_cleanup(
    sweep: Callable[[], Awaitable[int]],
    interval_seconds: float = DEFAULT_CLEANUP_INTERVAL_SECONDS,
    sleep=asyncio.sleep,
) -> None:
    while True:
        try:
            closed = await sweep()
            logger.info("stale session sweep closed {} sessions", closed)
        except Exception:
            logger.exception("stale session sweep failed")
        await sleep(interval_seconds)
