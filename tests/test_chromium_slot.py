"""One automation Chromium per process: the applier and browser fetchers share app.core.http.chromium_slot."""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from app.core.http import ChromiumSlot, chromium_slot
from app.sources.base import SourceError
from app.sources import web


async def test_slot_serialises_tasks():
    slot = ChromiumSlot()
    order = []

    async def use(name):
        async with slot.hold():
            order.append(f"{name} in")
            await asyncio.sleep(0.01)
            order.append(f"{name} out")

    await asyncio.gather(use("a"), use("b"))
    assert order == ["a in", "a out", "b in", "b out"]
    assert not slot.busy


async def test_slot_is_reentrant_in_one_task():
    """A task holding the slot that starts a fetcher must not wait for itself."""
    slot = ChromiumSlot()
    async with slot.hold():
        await asyncio.wait_for(slot.acquire(), timeout=1)  # would deadlock with a plain Lock
        slot.release()
        assert slot.busy
    assert not slot.busy
    slot.release()  # an extra release is harmless
    async with slot.hold():
        assert slot.busy


class _FakeBrowser:
    def __init__(self, log, fail=False):
        self.log, self.fail = log, fail

    async def new_context(self, **kwargs):
        if self.fail:
            raise RuntimeError("no display")
        return object()

    async def close(self):
        self.log.append("browser closed")


def _fake_playwright(monkeypatch, log, fail=False):
    class Chromium:
        async def launch(self, **kwargs):
            log.append(f"launch busy={chromium_slot.busy}")
            return _FakeBrowser(log, fail)

    class PW:
        chromium = Chromium()

        async def stop(self):
            log.append("pw stopped")

    class Starter:
        async def start(self):
            return PW()

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = Starter
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)


async def test_browser_fetcher_holds_the_slot_until_closed(monkeypatch):
    log: list[str] = []
    _fake_playwright(monkeypatch, log)
    fetcher = web.BrowserFetcher()
    await fetcher._ensure()
    assert log == ["launch busy=True"] and chromium_slot.busy

    async def applier():
        async with chromium_slot.hold():
            log.append("applier runs")

    waiting = asyncio.create_task(applier())
    await asyncio.sleep(0.01)
    assert "applier runs" not in log  # waits for the fetcher's browser
    await fetcher.close()
    await asyncio.wait_for(waiting, timeout=1)
    assert log[-1] == "applier runs" and not chromium_slot.busy
    await fetcher.close()  # closing twice does not release someone else's slot
    assert not chromium_slot.busy


async def test_browser_fetcher_releases_the_slot_when_launch_fails(monkeypatch):
    _fake_playwright(monkeypatch, [], fail=True)
    fetcher = web.BrowserFetcher()
    with pytest.raises(SourceError, match="Не удалось запустить браузер"):
        await fetcher._ensure()
    assert not chromium_slot.busy


async def test_hh_applier_launches_inside_the_slot(monkeypatch, env, tmp_path):
    from app.apply.base import ApplyRequest
    from app.apply.hh_browser import HHBrowserApplier

    env.data_dir = str(tmp_path)
    seen = []

    class PW:
        class chromium:  # noqa: N801 - mimics the playwright attribute
            @staticmethod
            async def launch(**kwargs):
                seen.append(chromium_slot.busy)
                raise module.Error("boom")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    module = types.ModuleType("playwright.async_api")
    module.Error = type("Error", (Exception,), {})
    module.async_playwright = PW
    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    applier = HHBrowserApplier()
    applier.save_session(1, {"cookies": [{"name": "hhtoken", "value": "x", "domain": ".hh.ru", "path": "/"}],
                             "origins": []})
    result = await applier.apply(ApplyRequest(user_id=1, source="hh", external_id="123", url="", letter="x"))
    assert seen == [True] and result.transient and not chromium_slot.busy
