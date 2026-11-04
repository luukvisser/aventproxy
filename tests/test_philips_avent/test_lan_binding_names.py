"""camera_names (lan_bindings.py): the names a camera may be bound by, from the
device registry, looked up per config entry where Home Assistant can."""

import sys
import types
from types import SimpleNamespace

from .ha_stubs import load

DEVICE = SimpleNamespace(name="Nursery", name_by_user="Upstairs")


class NewRegistry:
    def __init__(self):
        self.calls = []

    def async_get_device_by_identifier(self, identifier, entry_id):
        self.calls.append((identifier, entry_id))
        return DEVICE if identifier[1] == "abc" else None

    def async_get_device(self, identifiers):  # deprecated: must not be used
        raise AssertionError("async_get_device by identifiers is deprecated")


class OldRegistry:
    def async_get_device(self, identifiers):
        return DEVICE if ("philips_avent", "abc") in identifiers else None


def _load(monkeypatch, registry):
    module = types.ModuleType("homeassistant.helpers.device_registry")
    module.async_get = lambda hass: registry
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", module)
    bindings = load(monkeypatch, "lan_bindings")
    monkeypatch.setattr(bindings, "dr", module)
    return bindings


def test_per_entry_lookup(monkeypatch):
    registry = NewRegistry()
    bindings = _load(monkeypatch, registry)
    names = bindings.camera_names(None, "entry1", {"abc": "Cloud name", "def": "Other"})
    assert names == {"abc": ["Cloud name", "Nursery", "Upstairs"], "def": ["Other"]}
    assert registry.calls == [(("philips_avent", "abc"), "entry1"), (("philips_avent", "def"), "entry1")]


def test_older_home_assistant(monkeypatch):
    bindings = _load(monkeypatch, OldRegistry())
    assert bindings.camera_names(None, "entry1", {"abc": "Cloud name"}) == {"abc": ["Cloud name", "Nursery", "Upstairs"]}


def test_every_lan_addresses_call_passes_the_entry():
    # No test loads __init__.py, so a call missed when the entry was added
    # only showed at setup (TypeError, 04-11-2026). Check the calls' arity.
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "custom_components" / "philips_avent"
    calls = [
        node
        for path in root.glob("*.py")
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call) and getattr(node.func, "id", None) in ("lan_addresses", "camera_names")
    ]
    assert calls
    for call in calls:
        want = 4 if call.func.id == "lan_addresses" else 3
        assert len(call.args) + len(call.keywords) == want, ast.unparse(call)
