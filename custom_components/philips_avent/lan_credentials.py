"""LAN credentials the bridge needs, gathered per camera."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

try:
    from .const import STREAM_SOURCE_WEBRTC
    from .lan_policy import KEYED_PROTOCOL_MIN_VERSION, ordered_hosts, parse_protocol_version
    from .payload import LAN_CLARITY_HD
except ImportError:  # loaded standalone by unit tests, without the HA package
    from const import STREAM_SOURCE_WEBRTC
    from lan_policy import KEYED_PROTOCOL_MIN_VERSION, ordered_hosts, parse_protocol_version
    from payload import LAN_CLARITY_HD

_LOGGER = logging.getLogger(__name__)

# Bound on fetching a missing P2P password again, so a rewrite during a cloud
# outage still writes the new address promptly.
REFETCH_TIMEOUT_SECONDS = 15.0


async def fetch_p2p_passwords(api: Any, camera_ids: list[str], previous: dict[str, str]) -> dict[str, str]:
    """The P2P password per camera, fetched once per setup.

    Only the RTC config carries it. A failed call keeps the previous value (from
    the existing bridge JSON) instead of blanking it, so a cloud outage does not
    turn LAN streaming off. Never None.
    """
    out: dict[str, str] = {}
    for cam_id in camera_ids:
        try:
            out[cam_id] = (await api.get_rtc_config(cam_id)).get("password") or previous.get(cam_id, "")
        except Exception as err:  # noqa: BLE001 - no RTC config only means no fresh password for this camera
            _LOGGER.warning("No RTC config for %s, keeping the previous password if any: %s", cam_id, err)
            out[cam_id] = previous.get(cam_id, "")
    return out


async def fetch_missing_passwords(
    api: Any, camera_ids: list[str], passwords: dict[str, str], timeout: float = REFETCH_TIMEOUT_SECONDS
) -> dict[str, str]:
    """Fetch again the P2P passwords still missing; only the ones that came back.

    A first setup while the RTC config call fails has no earlier file to keep a
    password from, so LAN streaming stayed off for that whole run. A later
    rewrite calls this. No call at all when nothing is missing.
    """
    missing = [cam_id for cam_id in camera_ids if not passwords.get(cam_id)]
    if not missing:
        return {}
    try:
        async with asyncio.timeout(timeout):
            fetched = await fetch_p2p_passwords(api, missing, {})
    except TimeoutError:
        _LOGGER.warning("Timed out fetching the P2P password again for %s", ", ".join(missing))
        return {}
    return {cam_id: password for cam_id, password in fetched.items() if password}


def local_stream_possible(
    lan_streaming: bool, local_key: str | None, protocol: str, bound: str | None = None,
) -> bool:
    """Whether the bridge can stream this camera over the LAN, as far as setup knows.

    LAN streaming on and a local key. The address and the protocol may come only
    later (a scan, the first session), so an unknown protocol counts as
    possible. Below 3.4 only with an address bound to the camera (``bound``):
    3.3 proves no key in its handshake, so the bridge proves the camera by its
    first answer and tries 3.3 on that one address only (pkg/rtsp/lan_exclusive.go).
    """
    if not lan_streaming or not local_key:
        return False
    version = parse_protocol_version(protocol)
    return version is None or version >= KEYED_PROTOCOL_MIN_VERSION or bool(bound)


def password_fetch_ids(
    camera_ids: list[str],
    previous: dict[str, dict],
    keys: dict[str, str | None],
    *,
    skip: set[str] | frozenset[str] = frozenset(),
    lan_preferred: bool = False,
) -> list[str]:
    """The cameras whose P2P password setup asks the cloud for.

    Not those in ``skip`` (set to "WebRTC only": the bridge gets no password
    for them). In Prefer the LAN, also not one whose previous bridge config has
    a password for the same local key, so a restart makes no cloud call for
    it; a changed key means the monitor was paired again, and its password may
    have changed with it.
    """
    out = []
    for cam_id in camera_ids:
        if cam_id in skip:
            continue
        seed = previous.get(cam_id, {})
        if lan_preferred and seed.get("p2p_password") and seed.get("local_key") == keys.get(cam_id):
            continue
        out.append(cam_id)
    return out


def lan_credentials(
    coordinators: dict,
    passwords: dict[str, str],
    configured: dict[str, list[str]] | None = None,
    clarities: dict[str, int] | None = None,
    sources: dict[str, str] | None = None,
    bound: dict[str, str] | None = None,
) -> dict[str, dict]:
    """Map camera id to ``local_key``, ``p2p_password``, ``lan_ip``, ``lan_protocol``,
    ``lan_hosts``, ``lan_clarity`` and ``stream_source``.

    No API call, so an address-triggered rewrite works while the cloud is down.
    The key comes from the latest cloud device info, so a rotated key wins over
    the stored one. ``lan_ip`` is the last published address, never the live one,
    and ``lan_protocol`` the version published with it.

    ``lan_hosts`` are the camera's configured addresses (``configured``, its
    bound one first) without its published one and without the ones published
    for the entry's other cameras: a wrong-key handshake from the bridge holds
    one of the other camera's three LAN sessions for about 10 s
    (lan_policy.AddressClaims). ``clarities`` and ``sources`` are each
    camera's "Local stream quality" and "Stream source" choices. A camera set
    to "WebRTC only" gets neither secret: its bridge never streams it locally,
    and the file is copied by every backup.

    A camera on 3.3 streams locally only on the address ``bound`` to it
    (local_stream_possible): ``lan_hosts`` is then that one address, which the
    bridge takes as the binding; without one it gets neither secret either.
    """
    configured = configured or {}
    clarities = clarities or {}
    sources = sources or {}
    bound = bound or {}
    published = {cam_id: coordinator.lan_ip for cam_id, coordinator in coordinators.items() if coordinator.lan_ip}
    out: dict[str, dict] = {}
    for cam_id, coordinator in coordinators.items():
        lan_ip = coordinator.lan_ip or ""
        others = {ip for cid, ip in published.items() if cid != cam_id}
        hosts = [ip for ip in ordered_hosts(configured.get(cam_id, []), None, others) if ip != lan_ip]
        no_secrets = sources.get(cam_id) == STREAM_SOURCE_WEBRTC
        version = parse_protocol_version(coordinator.lan_protocol or "")
        if version is not None and version < KEYED_PROTOCOL_MIN_VERSION:
            if bound.get(cam_id):
                hosts = [bound[cam_id]]
            else:
                no_secrets = True
        out[cam_id] = {
            "local_key": "" if no_secrets else coordinator.device_info.get("localKey") or coordinator.local_key or "",
            "p2p_password": "" if no_secrets else passwords.get(cam_id) or "",
            "lan_ip": lan_ip,
            "lan_protocol": coordinator.lan_protocol or "",
            "lan_hosts": hosts,
            "lan_clarity": clarities.get(cam_id) or LAN_CLARITY_HD,
            "stream_source": sources.get(cam_id) or "",
        }
    return out
