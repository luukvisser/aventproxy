"""What a start without the Tuya cloud needs, per camera, and how it is stored.

After every successful cloud fetch the coordinator keeps the camera's localKey,
the few device fields the entities use, the last DPS snapshot and the address
and protocol of the last working LAN session. Home Assistant's private storage
helper holds it (`.storage/philips_avent.<entry_id>`, mode 600), not the config
entry, so it never shows up in the entry, the diagnostics dump or the bridge
config. The localKey alone is enough to control the camera on the LAN, so
nothing here may be logged: `describe` gives a loggable summary instead.

No Home Assistant imports here, so the unit tests can load it directly;
store.py wraps it in a Store.
"""
from __future__ import annotations

from typing import Any

try:
    from .const import DOMAIN
    from .lan_policy import parse_protocol_version
except ImportError:  # loaded standalone by unit tests
    from const import DOMAIN
    from lan_policy import parse_protocol_version

CACHE_VERSION = 1
CACHE_MINOR_VERSION = 1

# Device fields kept from tuya.m.device.get. productId names the model on the
# device page; name and verSw are kept so a later change can show them without
# the cloud. Never the whole record: it carries the household's public IP.
CACHED_DEVICE_FIELDS = ("productId", "name", "verSw")

# A changed key, device field or LAN address is written at once; a DPS snapshot
# that is all that changed waits this long, since a cloud poll lands every 30 to
# 120 seconds and SD cards on small hosts should not be written that often.
DPS_SAVE_INTERVAL = 600.0

# DPS that describe what the camera is doing at the moment, not how it is set:
# the lullaby state and command (246, 201), the lullaby and night light time
# left (245, 242), two-way audio (253), alert events (212, 250, 141) and the
# SenseIQ live status (3). They are not restored on a start from the cache.
# This firmware does not answer a status query, so a restored value stays
# until the camera happens to push that DPS again; with LAN preferred there
# is no poll to correct it either, and a lullaby that played when the
# snapshot was taken showed as playing, 90 minutes left, after every restart.
VOLATILE_DPS = frozenset({"3", "141", "201", "212", "242", "245", "246", "250", "253"})


def cache_key(entry_id: str) -> str:
    """Storage key for one config entry's cache."""
    return f"{DOMAIN}.{entry_id}"


def build_entry(
    local_key: str | None,
    device_info: dict | None,
    dps: dict | None,
    lan_ip: str | None = None,
    lan_version: float | None = None,
) -> dict | None:
    """One camera's cache entry, or None without a key (nothing to start with)."""
    if not local_key or not isinstance(local_key, str):
        return None
    device = {field: (device_info or {})[field] for field in CACHED_DEVICE_FIELDS if field in (device_info or {})}
    entry: dict[str, Any] = {
        "localKey": local_key,
        "device": device,
        "dps": dict(dps or {}),
    }
    if lan_ip:
        entry["lan_ip"] = lan_ip
    if parse_protocol_version(lan_version) is not None:
        entry["lan_version"] = parse_protocol_version(lan_version)
    return entry


def _valid_entry(entry: Any) -> dict | None:
    """The entry as stored, or None when it is not one this version can use."""
    if not isinstance(entry, dict):
        return None
    key = entry.get("localKey")
    if not isinstance(key, str) or not key:
        return None
    device = entry.get("device") if isinstance(entry.get("device"), dict) else {}
    dps = entry.get("dps") if isinstance(entry.get("dps"), dict) else {}
    lan_ip = entry.get("lan_ip") if isinstance(entry.get("lan_ip"), str) else None
    return build_entry(key, device, dps, lan_ip, entry.get("lan_version"))


def parse_cache(raw: Any) -> dict[str, dict]:
    """Camera id -> entry from what the store loaded, dropping whatever is malformed.

    A cache is disposable: a bad entry costs one camera its LAN-only start, not
    the integration its setup.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("cameras"), dict):
        return {}
    cameras = {}
    for cam_id, entry in raw["cameras"].items():
        valid = _valid_entry(entry)
        if valid is not None:
            cameras[str(cam_id)] = valid
    return cameras


def dump_cache(cameras: dict[str, dict]) -> dict:
    """What the store writes."""
    return {"cameras": cameras}


def migrate_cache(old_major: int, old_minor: int, data: Any) -> dict:
    """Bring stored data to CACHE_VERSION.

    Minor versions only ever add optional fields, so their data passes through.
    A major version this code does not know is dropped rather than guessed at;
    the next cloud fetch fills the cache again.
    """
    if old_major == CACHE_VERSION:
        return dump_cache(parse_cache(data))
    return dump_cache({})


def usable_for_fallback(entry: dict | None) -> bool:
    """Whether the entry can start a camera on the LAN while the cloud is away."""
    return bool(entry and entry.get("localKey"))


def usable_for_lan_preferred(entry: dict | None) -> bool:
    """Whether "LAN preferred" can start the camera from the entry without the cloud.

    It needs the key, the device details the entities use and an address to
    connect to, since the broadcast scan may never reach Home Assistant.
    """
    return bool(
        usable_for_fallback(entry)
        and "productId" in (entry.get("device") or {})
        and entry.get("lan_ip")
    )


def restorable_dps(entry: dict | None) -> dict:
    """The cached DPS a start from the cache shows, without the VOLATILE_DPS."""
    dps = (entry or {}).get("dps") or {}
    return {dp: value for dp, value in dps.items() if dp not in VOLATILE_DPS}


def device_info_from_entry(entry: dict | None) -> dict:
    """The coordinator's device_info for a start from the cache. Never the key."""
    return dict((entry or {}).get("device") or {})


def save_due(old: dict | None, new: dict | None, last_saved: float | None, now: float,
             interval: float = DPS_SAVE_INTERVAL) -> bool:
    """Whether `new` should be written over `old`.

    Anything but the DPS snapshot is written as soon as it changes; a DPS-only
    change waits for `interval` since the last write.
    """
    if new is None or new == old:
        return False
    if old is None or {k: v for k, v in old.items() if k != "dps"} != {k: v for k, v in new.items() if k != "dps"}:
        return True
    return last_saved is None or now - last_saved >= interval


def describe(entry: dict | None) -> str:
    """A loggable summary of an entry: which parts are there, never their values."""
    if not entry:
        return "no cache"
    parts = ["key" if entry.get("localKey") else "no key"]
    parts.append(f"{len(entry.get('dps') or {})} DPS")
    parts.append("LAN address" if entry.get("lan_ip") else "no LAN address")
    if entry.get("lan_version"):
        parts.append(f"protocol {entry['lan_version']}")
    return ", ".join(parts)
