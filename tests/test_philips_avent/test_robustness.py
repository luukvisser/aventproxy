"""Failures that did not recover, or alerts that could be lost (review of 2026-11-04).

These paths run inside Home Assistant (coordinator scheduling, the LAN
listener task, setup), which this test suite does not load, so the checks
read the source for the shape of each fix.
"""

import ast
from pathlib import Path

COMPONENT = Path(__file__).resolve().parents[2] / "custom_components" / "philips_avent"


def _source(name: str) -> str:
    return (COMPONENT / name).read_text(encoding="utf-8")


def test_lan_and_command_updates_do_not_reschedule_the_cloud_poll():
    # Home Assistant's async_set_updated_data plans the next poll a whole
    # interval later: a camera pushing more often than that was never polled.
    coordinator = _source("coordinator.py")
    # Pushes go through _show_lan_state (upstream f53396a); command echoes
    # (_apply_optimistic) no longer reschedule it either. Only the cloud
    # resync after a LAN drop is a cloud fetch that may move it.
    assert coordinator.count("self.async_set_updated_data(") == 1
    assert 'resynced from the cloud after the LAN came back", self.camera_name)\n        self.async_set_updated_data(data)' in coordinator
    assert "def _show_lan_state(self" in coordinator
    assert "def _apply_optimistic(self" in coordinator


def test_the_lan_listener_restarts_after_an_unexpected_error():
    tree = ast.parse(_source("lan.py"))
    run = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run")
    handlers = [h for n in ast.walk(run) if isinstance(n, ast.Try) for h in n.handlers]
    names = {ast.unparse(h.type) for h in handlers if h.type is not None}
    assert {"asyncio.CancelledError", "Exception"} <= names
    assert "await self._listen()" in ast.unparse(run)


def test_rssi_without_a_record_keeps_the_last_value():
    assert "if isinstance(rssi_data, dict):" in _source("coordinator.py")


def test_diagnostics_work_for_an_entry_that_is_not_loaded():
    assert "hass.data.get(DOMAIN, {}).get(entry.entry_id, {})" in _source("diagnostics.py")


def test_a_failed_discovery_retries_the_entry():
    init = _source("__init__.py")
    assert 'raise ConfigEntryNotReady(f"camera discovery failed: {err}") from err' in init


def test_only_an_options_change_reloads_the_entry():
    # Every update of the entry calls the update listener, data too: a reauth
    # (which reloads by itself) reloaded the entry twice.
    tree = ast.parse(_source("__init__.py"))
    listener = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_async_options_updated")
    body = ast.unparse(listener)
    assert "loaded.get('options') == dict(entry.options)" in body
    assert '"options": dict(entry.options),' in _source("__init__.py")


def test_new_cameras_in_the_account_are_looked_for_after_setup():
    init = _source("__init__.py")
    assert "entry.async_create_background_task(\n            hass, _look_for_new_cameras(hass, entry, api)" in init
    tree = ast.parse(init)
    look = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_look_for_new_cameras")
    body = ast.unparse(look)
    assert "new_cameras(stored, discovered)" in body
    assert "async_schedule_reload(entry.entry_id)" in body
    # A failed discovery is caught: the entry stays as it is.
    assert any(isinstance(n, ast.Try) for n in ast.walk(look))


def test_new_cameras_are_only_additions():
    from account_cameras import new_cameras, stored_camera

    stored = [{"id": "a", "name": "Nursery", "product_id": "p1"}]
    discovered = [
        {"devId": "a", "name": "Nursery", "productId": "p1"},
        {"deviceId": "b", "deviceName": "Bedroom", "productKey": "p2"},
        {"devId": "b", "name": "Bedroom again"},
        {"name": "no id"},
    ]
    assert new_cameras(stored, discovered) == [{"id": "b", "name": "Bedroom", "product_id": "p2"}]
    # A camera missing from a discovery stays: nothing to add, nothing removed.
    assert new_cameras(stored, []) == []
    assert stored_camera({"devId": "c"}) == {"id": "c", "name": "camera", "product_id": ""}
