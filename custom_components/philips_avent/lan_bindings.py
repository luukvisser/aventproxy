"""Per-camera LAN addresses from the options ("camera=address"), resolved to cameras.

The pure parsing and matching are in lan_policy (parse_lan_hosts,
resolve_lan_bindings); this module only gathers the names a user may use for a
camera: the cloud's name, and the name Home Assistant shows for its device,
including one the user gave it.
"""
from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr

from .const import DOMAIN
from .lan_policy import parse_lan_hosts, resolve_lan_bindings, shared_hosts_for


def camera_names(hass: HomeAssistant, entry_id: str, cloud_names: dict[str, str]) -> dict[str, list[str]]:
    """{camera id: the names it may be bound by} for the cameras of an entry."""
    registry = dr.async_get(hass)
    names: dict[str, list[str]] = {}
    for camera, cloud_name in cloud_names.items():
        found = [cloud_name]
        # async_get_device by identifiers is deprecated (HA 2026.10 logs it:
        # identifiers are unique per config entry only); the replacement
        # takes the entry. Older HA has only the first.
        if hasattr(registry, "async_get_device_by_identifier"):
            device = registry.async_get_device_by_identifier((DOMAIN, camera), entry_id)
        else:
            device = registry.async_get_device(identifiers={(DOMAIN, camera)})
        if device is not None:
            found += [device.name or "", device.name_by_user or ""]
        names[camera] = [n for n in dict.fromkeys(found) if n]
    return names


def lan_addresses(
    hass: HomeAssistant, entry_id: str, option: str | None, cloud_names: dict[str, str]
) -> tuple[dict[str, list[str]], dict[str, str], list[str]]:
    """Per camera its shared addresses, its bound address, and the bindings that name no camera.

    An invalid option gives nothing to try (the options flow refuses one).
    """
    parsed = parse_lan_hosts(option)
    if parsed is None:
        return {camera: [] for camera in cloud_names}, {}, []
    shared, bound = parsed
    resolved, unresolved = resolve_lan_bindings(bound, camera_names(hass, entry_id, cloud_names))
    per_camera = {camera: shared_hosts_for(shared, resolved, camera) for camera in cloud_names}
    return per_camera, resolved, unresolved
