import asyncio

import pytest

from conic import catalog
from conic.catalog import CATALOG_PROVIDERS, run_periodic_sync, sync_experiential_once
from conic.services.models import ModelCatalogEntry


class FakeStorage:
    def __init__(self):
        self.saved: list[tuple[str, list]] = []

    def save_model_catalog(self, provider, entries):
        self.saved.append((provider, entries))


def sleep_n_times_then_cancel(n):
    calls = []

    async def fake_sleep(seconds):
        calls.append(seconds)
        if len(calls) >= n:
            raise asyncio.CancelledError

    return fake_sleep, calls


def counting_sync(fail=False):
    calls = []

    async def sync():
        calls.append(1)
        if fail:
            raise RuntimeError("boom")
        return True

    return sync, calls


async def test_run_periodic_sync_does_not_sync_before_the_first_sleep():
    sync, synced = counting_sync()
    fake_sleep, calls = sleep_n_times_then_cancel(1)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(sync, sleep=fake_sleep)

    assert synced == []
    assert calls == [3600]


async def test_run_periodic_sync_syncs_after_each_sleep():
    sync, synced = counting_sync()
    fake_sleep, calls = sleep_n_times_then_cancel(2)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(sync, sleep=fake_sleep)

    assert len(synced) == 1
    assert calls == [3600, 3600]


async def test_run_periodic_sync_resyncs_on_every_interval():
    sync, synced = counting_sync()
    fake_sleep, calls = sleep_n_times_then_cancel(4)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(sync, interval_seconds=1, sleep=fake_sleep)

    assert len(synced) == 3
    assert calls == [1, 1, 1, 1]


async def test_run_periodic_sync_keeps_looping_after_a_failed_sync():
    sync, synced = counting_sync(fail=True)
    fake_sleep, calls = sleep_n_times_then_cancel(3)

    with pytest.raises(asyncio.CancelledError):
        await run_periodic_sync(sync, sleep=fake_sleep)

    assert len(synced) == 2
    assert len(calls) == 3


async def test_sync_experiential_once_saves_entries_under_experiential_provider(monkeypatch):
    entries = [ModelCatalogEntry(slug="claude-opus-5.5", provider="experiential")]

    async def fake_fetch():
        return entries

    monkeypatch.setattr(catalog, "fetch_experiential_models", fake_fetch)
    storage = FakeStorage()

    ok = await sync_experiential_once(storage, object())

    assert ok is True
    assert storage.saved == [("experiential", entries)]


async def test_sync_experiential_once_returns_false_and_saves_nothing_on_failure(monkeypatch):
    async def fake_fetch():
        raise RuntimeError("down")

    monkeypatch.setattr(catalog, "fetch_experiential_models", fake_fetch)
    storage = FakeStorage()

    ok = await sync_experiential_once(storage, object())

    assert ok is False
    assert storage.saved == []


def test_catalog_providers_are_openrouter_then_experiential():
    assert [p.name for p in CATALOG_PROVIDERS] == ["openrouter", "experiential"]


async def test_sync_experiential_once_returns_false_and_saves_nothing_on_empty_fetch(monkeypatch):
    async def fake_fetch():
        return []

    monkeypatch.setattr(catalog, "fetch_experiential_models", fake_fetch)
    storage = FakeStorage()

    ok = await sync_experiential_once(storage, object())

    assert ok is False
    assert storage.saved == []
