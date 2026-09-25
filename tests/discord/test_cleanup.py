import asyncio

import pytest

from conic.discord.cleanup import run_periodic_cleanup


def sleep_n_times_then_cancel(n):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= n:
            raise asyncio.CancelledError

    return fake_sleep, calls


async def test_run_periodic_cleanup_sweeps_immediately_then_every_interval():
    sweeps = []

    async def sweep():
        sweeps.append(1)
        return 0

    fake_sleep, calls = sleep_n_times_then_cancel(3)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_cleanup(sweep, sleep=fake_sleep)

    assert len(sweeps) == 3
    assert calls == [3600, 3600, 3600]


async def test_run_periodic_cleanup_survives_a_failing_sweep():
    attempts = []

    async def flaky_sweep():
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("boom")
        return 1

    fake_sleep, _ = sleep_n_times_then_cancel(2)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_cleanup(flaky_sweep, sleep=fake_sleep)

    assert len(attempts) == 2
