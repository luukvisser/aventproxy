"""The account's cameras as the config entry keeps them.

The config flow stores the cameras it finds in the entry's data, and setup
starts those. Kept free of Home Assistant imports so the unit tests can load
it.
"""
from __future__ import annotations


def stored_camera(discovered: dict) -> dict:
    """The entry's record of one camera from the cloud's device list."""
    return {
        "id": discovered.get("devId") or discovered.get("deviceId"),
        "name": discovered.get("name") or discovered.get("deviceName", "camera"),
        "product_id": discovered.get("productId") or discovered.get("productKey") or "",
    }


def new_cameras(stored: list[dict], discovered: list[dict]) -> list[dict]:
    """Records for the discovered cameras the entry does not have yet.

    Only additions: a camera missing from one discovery (offline, or a
    partial answer from the cloud) stays in the entry.
    """
    known = {cam.get("id") for cam in stored}
    added = []
    for dev in discovered:
        cam = stored_camera(dev)
        if cam["id"] and cam["id"] not in known:
            known.add(cam["id"])
            added.append(cam)
    return added
