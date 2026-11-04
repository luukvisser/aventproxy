"""Diagnostics for Philips Avent Baby Monitor."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

try:
    from .const import DOMAIN
    from .redact import redact_secrets
except ImportError:  # loaded standalone by unit tests
    from const import DOMAIN
    from redact import redact_secrets

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    # An entry in setup retry has no runtime data, and that is when
    # diagnostics are wanted most: the redacted entry still comes back.
    data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    coordinators = data.get("coordinators", {})

    diag: dict[str, Any] = {
        "config_entry": redact_secrets(dict(entry.data)),
        "devices": {},
    }

    for cam_id, coordinator in coordinators.items():
        diag["devices"][cam_id] = {
            "name": coordinator.camera_name,
            "dps": coordinator.data,
            "lan_connected": coordinator.lan_connected,
            # The offline cache (store.py) is left out on purpose: it holds the
            # local key, and the device_info below is what the entities use.
            "cloud_connected": coordinator.cloud_available,
            "cloud_failure": coordinator.cloud_failure,
            "update_interval": str(coordinator.update_interval),
            "rssi": coordinator.rssi,
            "device_info": redact_secrets(coordinator.device_info) if coordinator.device_info else None,
        }

    return diag
