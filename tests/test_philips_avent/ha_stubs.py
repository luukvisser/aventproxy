"""Load lan.py and coordinator.py without Home Assistant or tinytuya installed.

Most tests import the integration's pure modules directly (see conftest.py).
The LAN client and the coordinator import Home Assistant and use relative
imports, so they are loaded here as part of a stand-in package, with the few
Home Assistant names they use replaced by small fakes. The fakes model only
what the code under test relies on; anything else fails loudly.
"""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

PKG_DIR = Path(__file__).parent.parent.parent / "custom_components" / "philips_avent"
PKG_NAME = "_avent_under_test"


class ConfigEntryNotReady(Exception):
    pass


class ConfigEntryAuthFailed(Exception):
    pass


class HomeAssistantError(Exception):
    pass


class UpdateFailed(Exception):
    pass


class FakeHass:
    """Runs executor jobs inline and keeps background tasks so tests can await them."""

    def __init__(self):
        self.tasks: list[asyncio.Task] = []
        self.timers: list = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def async_create_background_task(self, coro, name=None, eager_start=False):
        task = asyncio.get_running_loop().create_task(coro)
        self.tasks.append(task)
        return task

    def async_create_task(self, coro, name=None, eager_start=False):
        return self.async_create_background_task(coro, name)

    async def settle(self):
        """Wait for every task started so far (and the ones those start)."""
        while pending := [t for t in self.tasks if not t.done()]:
            await asyncio.gather(*pending, return_exceptions=True)


class FakeDataUpdateCoordinator:
    """The parts of Home Assistant's DataUpdateCoordinator the integration uses.

    async_config_entry_first_refresh mirrors Home Assistant: an auth failure
    passes through, any other failure becomes ConfigEntryNotReady.
    """

    def __init__(self, hass, logger, name=None, update_interval=None):
        self.hass = hass
        self.logger = logger
        self.name = name
        self.update_interval = update_interval
        self.data = None
        self.last_update_success = True
        self.listener_updates = 0

    async def async_config_entry_first_refresh(self):
        try:
            self.data = await self._async_update_data()
        except ConfigEntryAuthFailed:
            self.last_update_success = False
            raise
        except Exception as err:
            self.last_update_success = False
            raise ConfigEntryNotReady() from err
        self.last_update_success = True

    async def async_refresh(self):
        """One scheduled poll."""
        try:
            self.data = await self._async_update_data()
        except UpdateFailed:
            self.last_update_success = False
        else:
            self.last_update_success = True
        self.async_update_listeners()

    async def async_request_refresh(self):
        await self.async_refresh()

    def async_set_updated_data(self, data):
        self.data = data
        self.last_update_success = True
        self.async_update_listeners()

    def async_update_listeners(self):
        self.listener_updates += 1


def _callback(func):
    return func


def _call_later(hass, delay, action):
    hass.timers.append((delay, action))
    return lambda: None


def _track_time_interval(hass, action, interval):
    hass.timers.append((interval, action))
    return lambda: None


class FakeStore:
    """Home Assistant's Store: a save waits on `gate` when a test sets one."""

    def __init__(self, hass, version, key, private=False, minor_version=1):
        self.key = key
        self.data = None
        self.events = []
        self.gate = None

    async def async_load(self):
        return self.data

    async def async_save(self, data):
        self.events.append("save started")
        if self.gate is not None:
            await self.gate.wait()
        self.data = data
        self.events.append("saved")

    async def async_remove(self):
        self.data = None
        self.events.append("removed")


def _module(name, **attrs):
    module = types.ModuleType(name)
    module.__dict__.update(attrs)
    return module


def install(monkeypatch) -> None:
    """Put the fakes in sys.modules for the duration of one test."""
    stubs = {
        "homeassistant": _module("homeassistant"),
        "homeassistant.core": _module("homeassistant.core", HomeAssistant=FakeHass, callback=_callback),
        "homeassistant.exceptions": _module(
            "homeassistant.exceptions",
            ConfigEntryAuthFailed=ConfigEntryAuthFailed,
            ConfigEntryNotReady=ConfigEntryNotReady,
            HomeAssistantError=HomeAssistantError,
        ),
        "homeassistant.helpers": _module("homeassistant.helpers"),
        "homeassistant.helpers.event": _module(
            "homeassistant.helpers.event",
            async_call_later=_call_later,
            async_track_time_interval=_track_time_interval,
        ),
        "homeassistant.helpers.storage": _module("homeassistant.helpers.storage", Store=FakeStore),
        "homeassistant.helpers.update_coordinator": _module(
            "homeassistant.helpers.update_coordinator",
            DataUpdateCoordinator=FakeDataUpdateCoordinator,
            UpdateFailed=UpdateFailed,
        ),
        "homeassistant.util": _module("homeassistant.util"),
        "homeassistant.util.dt": _module(
            "homeassistant.util.dt",
            now=lambda: datetime.now(UTC),
            utcnow=lambda: datetime.now(UTC),
        ),
        # Replaced per test where a test talks to a device; importing lan.py
        # only needs the name to exist.
        "tinytuya": _module("tinytuya", Device=None, deviceScan=None),
    }
    for name, module in stubs.items():
        monkeypatch.setitem(sys.modules, name, module)

    package = types.ModuleType(PKG_NAME)
    package.__path__ = [str(PKG_DIR)]
    monkeypatch.setitem(sys.modules, PKG_NAME, package)
    # Drop modules a previous test loaded against other fakes.
    for name in [n for n in sys.modules if n.startswith(PKG_NAME + ".")]:
        monkeypatch.delitem(sys.modules, name)


def load(monkeypatch, module: str):
    install(monkeypatch)
    return importlib.import_module(f"{PKG_NAME}.{module}")


logging.getLogger(PKG_NAME).setLevel(logging.DEBUG)
