"""Philips Avent Baby Monitor integration for Home Assistant."""
from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import timedelta
from pathlib import Path

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .account_cameras import new_cameras
from .api import PhilipsAventAPI
from .bridge_share import LAN_SHARE_SEPARATE, LAN_SHARE_SHARED, dps_url, events_url, release_url
from .bridge_status import (
    STATUS_POLL_SECONDS,
    STATUS_REFRESH_COOLDOWN,
    STATUS_TIMEOUT,
    parse_status,
    status_url,
    webrtc_allowed,
)
from .cloud_policy import CLOUD_TIMEOUT, AccountCloud
from .const import (
    CONF_API_HOST,
    CONF_BRIDGE_API_TOKEN,
    CONF_BRIDGE_HOST,
    CONF_BRIDGE_PORT,
    CONF_COUNTRY_CODE,
    CONF_DEVICE_ID,
    CONF_ECODE,
    CONF_LAN_HOSTS,
    CONF_LAN_PREFERRED,
    CONF_LAN_SHARE,
    CONF_LAN_STREAMING,
    CONF_MAX_STREAM_MINUTES,
    CONF_PARTNER,
    CONF_SID,
    CONF_TALKBACK,
    CONF_UID,
    CONF_WEBRTC,
    CONF_WEBRTC_LAN_ONLY,
    DEFAULT_BRIDGE_HOST,
    DEFAULT_BRIDGE_PORT,
    DEFAULT_LAN_HOSTS,
    DEFAULT_LAN_PREFERRED,
    DEFAULT_LAN_SHARE,
    DEFAULT_LAN_STREAMING,
    DEFAULT_MAX_STREAM_MINUTES,
    DEFAULT_TALKBACK,
    DEFAULT_WEBRTC,
    DEFAULT_WEBRTC_LAN_ONLY,
    DOMAIN,
    STREAM_SOURCE_WEBRTC,
    TUYA_APP_KEY,
    TUYA_DEFAULT_COUNTRY_CODE,
    TUYA_PACKAGE_NAME,
    TUYA_SIGNING_KEY,
)
from .coordinator import PhilipsAventCoordinator
from .lan_bindings import lan_addresses
from .lan_credentials import (
    fetch_missing_passwords,
    fetch_p2p_passwords,
    lan_credentials,
    local_stream_possible,
    password_fetch_ids,
)
from .lan_policy import AddressClaims
from .lan_quality import LanQualityChoice, resolve_lan_quality
from .offline_cache import usable_for_lan_preferred
from .payload import (
    BRIDGE_CONFIG_PREFIX,
    bridge_config_filename,
    build_bridge_config,
    lan_clarity_for,
    orphan_bridge_configs,
    previous_lan_values,
    strip_stored_password,
    write_bridge_config_file,
)
from .region import DEFAULT_DATA_CENTER, api_host, api_url_for_host
from .store import offline_cache
from .stream_source import StreamSourceChoice

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.CAMERA, Platform.SENSOR, Platform.SWITCH, Platform.NUMBER, Platform.BUTTON, Platform.SELECT, Platform.BINARY_SENSOR]


def _entry_api_host(entry: ConfigEntry) -> str:
    """API host for this account, falling back to Central Europe.

    Entries created before data-center routing existed have no host stored; EU
    is the right fallback for them because that was the only host the
    integration ever used (issues #44, #58).
    """
    return entry.data.get(CONF_API_HOST) or api_host(DEFAULT_DATA_CENTER)


async def _write_bridge_config(
    hass: HomeAssistant, entry: ConfigEntry, api: PhilipsAventAPI, cameras: list, lan: dict
) -> None:
    """Write bridge config JSON for the add-on."""
    bridge_port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    bridge_config = build_bridge_config(
        signing_key=TUYA_SIGNING_KEY,
        sid=entry.data[CONF_SID],
        ecode=entry.data.get(CONF_ECODE, ""),
        partner=entry.data.get(CONF_PARTNER, ""),
        app_key=TUYA_APP_KEY,
        device_id=api.device_id,
        package_name=TUYA_PACKAGE_NAME,
        api_host=_entry_api_host(entry),
        talkback=entry.options.get(CONF_TALKBACK, DEFAULT_TALKBACK),
        uid=entry.data.get(CONF_UID, ""),
        lan_streaming=entry.options.get(CONF_LAN_STREAMING, DEFAULT_LAN_STREAMING),
        lan=lan,
        webrtc=entry.options.get(CONF_WEBRTC, DEFAULT_WEBRTC),
        max_stream_minutes=entry.options.get(CONF_MAX_STREAM_MINUTES, DEFAULT_MAX_STREAM_MINUTES),
        webrtc_lan_only=entry.options.get(CONF_WEBRTC_LAN_ONLY, DEFAULT_WEBRTC_LAN_ONLY),
        api_token=entry.data.get(CONF_BRIDGE_API_TOKEN),
        bridge_port=bridge_port,
        cameras=cameras,
    )
    bridge_path = Path(hass.config.path(bridge_config_filename(entry.entry_id)))
    # Owner-only: the file carries a live Tuya session and /config is mounted
    # by the Samba, File Editor and Terminal add-ons, and copied by backups.
    await hass.async_add_executor_job(
        write_bridge_config_file, bridge_path, bridge_config
    )
    _LOGGER.info(
        "Bridge config written to %s (port: %d, api host: %s)",
        bridge_path, bridge_port, bridge_config["api_host"],
    )

    legacy_path = Path(hass.config.path("philips_avent_bridge.json"))
    if await hass.async_add_executor_job(legacy_path.exists):
        await hass.async_add_executor_job(legacy_path.unlink)
        _LOGGER.info("Removed legacy bridge config %s", legacy_path)

    await _remove_orphan_bridge_configs(hass)


def _bridge_api_token(hass: HomeAssistant, entry: ConfigEntry) -> str:
    """The token for the bridge's shared-connection endpoints, made once."""
    token = entry.data.get(CONF_BRIDGE_API_TOKEN)
    if not token:
        token = secrets.token_urlsafe(32)
        hass.config_entries.async_update_entry(entry, data={**entry.data, CONF_BRIDGE_API_TOKEN: token})
    return token


def _connect_lan_share(hass: HomeAssistant, entry: ConfigEntry, coordinators: dict, status) -> None:
    """Tell each camera the "share the LAN connection" option and the bridge's stream paths."""
    host = entry.options.get(CONF_BRIDGE_HOST, DEFAULT_BRIDGE_HOST)
    port = entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT)
    mode = LAN_SHARE_SHARED if entry.options.get(CONF_LAN_SHARE, DEFAULT_LAN_SHARE) else LAN_SHARE_SEPARATE
    token = entry.data.get(CONF_BRIDGE_API_TOKEN)
    http = async_get_clientsession(hass)
    for cam_id, coordinator in coordinators.items():
        coordinator.configure_lan_share(
            mode, http, events_url(host, port, cam_id), dps_url(host, port, cam_id), token,
            release=release_url(host, port, cam_id),
        )

    @callback
    def _paths_changed() -> None:
        cams = (status.data or {}).get("cameras") or {} if status.last_update_success else {}
        for cam_id, coordinator in coordinators.items():
            cam = cams.get(cam_id) or {}
            coordinator.bridge_stream_changed(cam.get("path"), wants_session=cam.get("wants_session") is True)

    entry.async_on_unload(status.async_add_listener(_paths_changed))
    _paths_changed()


async def _bridge_status(hass: HomeAssistant, entry: ConfigEntry) -> DataUpdateCoordinator:
    """Poll the bridge's /status (bridge_status.py) for the "Stream path" sensors.

    A bridge that does not answer (an older one, or one restarting) makes
    those sensors unavailable and nothing else: the first refresh does not
    raise, so it never holds up the setup.
    """
    url = status_url(
        entry.options.get(CONF_BRIDGE_HOST, DEFAULT_BRIDGE_HOST),
        entry.options.get(CONF_BRIDGE_PORT, DEFAULT_BRIDGE_PORT),
    )
    session = async_get_clientsession(hass)

    async def fetch() -> dict:
        try:
            async with asyncio.timeout(STATUS_TIMEOUT):
                async with session.get(url) as response:
                    response.raise_for_status()
                    document = await response.json(content_type=None)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise UpdateFailed(f"bridge status at {url} unreachable: {err}") from err
        return {"cameras": parse_status(document), "webrtc": webrtc_allowed(document)}

    coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name="Philips Avent bridge status",
        update_method=fetch, update_interval=timedelta(seconds=STATUS_POLL_SECONDS),
        request_refresh_debouncer=Debouncer(hass, _LOGGER, cooldown=STATUS_REFRESH_COOLDOWN, immediate=True),
    )
    await coordinator.async_refresh()
    return coordinator


async def _remove_orphan_bridge_configs(hass: HomeAssistant) -> None:
    """Delete bridge config files belonging to entries that no longer exist.

    Re-adding the integration mints a new entry id, so the previous file stayed
    behind and the add-on could keep reading it: old session, old camera id, and
    a Tuya "No access" on every stream attempt (issue #52). Reinstalling made it
    worse, since each attempt left one more file.
    """
    config_dir = Path(hass.config.path())
    valid = {entry.entry_id for entry in hass.config_entries.async_entries(DOMAIN)}

    def _prune() -> list[str]:
        names = [p.name for p in config_dir.glob(f"{BRIDGE_CONFIG_PREFIX}*.json")]
        removed = []
        for name in orphan_bridge_configs(names, valid):
            try:
                (config_dir / name).unlink()
            except OSError as err:
                _LOGGER.warning("Could not remove stale bridge config %s: %s", name, err)
            else:
                removed.append(name)
        return removed

    removed = await hass.async_add_executor_job(_prune)
    for name in removed:
        _LOGGER.info(
            "Removed stale bridge config %s, it belonged to a config entry that no longer exists",
            name,
        )


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload integration when options change.

    Home Assistant calls this on every update of the entry, its data too: a
    reauth's new session, which reloads the entry itself (so it reloaded
    twice), or new cameras, which schedule their own reload. Those reload
    here no more.
    """
    loaded = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    if loaded is not None and loaded.get("options") == dict(entry.options):
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def _look_for_new_cameras(hass: HomeAssistant, entry: ConfigEntry, api: PhilipsAventAPI) -> None:
    """Add the cameras that joined the account after the entry was made.

    Setup starts the cameras the config flow stored, so a camera added to the
    account later never appeared without removing and re-adding the
    integration. One discovery after each setup, in the background; a
    failure (the cloud down) changes nothing, and new ones are stored and set
    up by one reload.
    """
    try:
        discovered = await api.discover_cameras()
    except Exception:  # best effort, the next setup looks again
        _LOGGER.debug("Looking for new cameras in the account failed", exc_info=True)
        return
    stored = entry.data.get("cameras", [])
    added = new_cameras(stored, discovered)
    if not added:
        return
    _LOGGER.info("Found %d new camera(s) in the account; reloading to add them", len(added))
    hass.config_entries.async_update_entry(entry, data={**entry.data, "cameras": [*stored, *added]})
    hass.config_entries.async_schedule_reload(entry.entry_id)


def _purge_stored_password(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove a password persisted by an older version of the config flow.

    Nothing ever read it back — reauth prompts for it again — so it was
    plaintext in .storage/core.config_entries and in every backup for no
    benefit. New entries no longer store it; this clears the ones that do.
    """
    cleaned = strip_stored_password(entry.data)
    if cleaned is None:
        return
    hass.config_entries.async_update_entry(entry, data=cleaned)
    _LOGGER.info("Removed the stored account password from the config entry; it was never used")


# A camera that did not start is tried again by reloading the entry this
# much later.
SKIPPED_CAMERA_RETRY_SECONDS = 300


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Philips Avent from a config entry."""
    _purge_stored_password(hass, entry)

    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=CLOUD_TIMEOUT))
    api = PhilipsAventAPI(
        session,
        sid=entry.data[CONF_SID],
        api_url=api_url_for_host(_entry_api_host(entry)),
        country_code=entry.data.get(CONF_COUNTRY_CODE) or TUYA_DEFAULT_COUNTRY_CODE,
        device_id=entry.data.get(CONF_DEVICE_ID),
    )
    if not entry.data.get(CONF_DEVICE_ID):
        # Entries created before the id was persisted: keep the one just
        # generated, so the bridge config stops changing on every restart
        # (issue #73).
        hass.config_entries.async_update_entry(
            entry, data={**entry.data, CONF_DEVICE_ID: api.device_id}
        )
        _LOGGER.info("Stored a stable device id for this account")

    # What a camera needs to start on the LAN without the cloud; filled by
    # every successful cloud fetch (offline_cache.py).
    cache = offline_cache(hass, entry.entry_id)
    # One view of the cloud for the account: every camera shares the session.
    account_cloud = AccountCloud()
    await cache.async_load()
    lan_preferred = entry.options.get(CONF_LAN_PREFERRED, DEFAULT_LAN_PREFERRED)

    # Use cameras stored in config entry (discovered during config flow)
    cameras = []
    stored_cameras = entry.data.get("cameras", [])
    if stored_cameras:
        cameras.extend(
            {
                "deviceId": cam["id"],
                "deviceName": cam["name"],
                "productId": cam.get("product_id", ""),
            }
            for cam in stored_cameras
        )
        _LOGGER.info("Using %d cameras from config entry", len(cameras))

        # Backfill productId for entries created before this field was tracked.
        # Guard on key presence in the stored entry, NOT on the in-memory value,
        # so post-fix entries with a genuinely empty productId (e.g. a device
        # that does not expose one) don't trigger a cloud call on every restart.
        # LAN preferred with a usable cache leaves the cloud alone here too;
        # the cache has the productId the bridge needs.
        if lan_preferred:
            for cam in cameras:
                cached = cache.get(cam["deviceId"])
                if not cam.get("productId") and usable_for_lan_preferred(cached):
                    cam["productId"] = cached["device"]["productId"]
        skip_backfill = lan_preferred and all(usable_for_lan_preferred(cache.get(cam["id"])) for cam in stored_cameras)
        if not skip_backfill and any("product_id" not in cam for cam in stored_cameras):
            patched = 0
            try:
                discovered = await api.discover_cameras()
                by_id = {(d.get("devId") or d.get("deviceId")): d for d in discovered}
                for cam in cameras:
                    if not cam.get("productId"):
                        disc = by_id.get(cam.get("deviceId"))
                        if disc:
                            new_id = disc.get("productId") or disc.get("productKey") or ""
                            if new_id:
                                cam["productId"] = new_id
                                patched += 1
                if patched:
                    updated_stored_cameras = [
                        {**stored_cam, "product_id": cam.get("productId", "")}
                        for stored_cam, cam in zip(stored_cameras, cameras)
                    ]
                    hass.config_entries.async_update_entry(
                        entry,
                        data={**entry.data, "cameras": updated_stored_cameras},
                    )
                    _LOGGER.info("Backfilled productId for %d camera(s) and persisted to config entry", patched)
                else:
                    _LOGGER.info("Backfill ran but no productId was recovered from Tuya discovery")
            except Exception:  # noqa: BLE001 - setup continues even if the backfill fails
                _LOGGER.warning(
                    "Could not backfill productId from Tuya API; SCD951 cameras may fail "
                    "to stream until HA restarts or the integration is reconfigured"
                )
    else:
        # Fallback: re-discover via API
        try:
            cameras = await api.discover_cameras()
        except Exception as err:
            # Retried, not given up: returning False would leave the entry
            # dead after one cloud hiccup at boot.
            _LOGGER.exception("Camera discovery failed")
            await session.close()
            raise ConfigEntryNotReady(f"camera discovery failed: {err}") from err

    if not cameras:
        _LOGGER.error("No cameras found. Reconfigure the integration to re-discover.")
        await session.close()
        return False

    bridge_path = Path(hass.config.path(bridge_config_filename(entry.entry_id)))

    def _read_previous() -> str:
        # Unreadable or undecodable both mean no seed. Raising here would fail
        # setup before the try below, leaking the aiohttp session.
        try:
            return bridge_path.read_text()
        except (OSError, ValueError):
            return ""

    previous = previous_lan_values(await hass.async_add_executor_job(_read_previous))

    coordinators: dict = {}
    passwords: dict[str, str] = {}
    # Per camera: the "Stream source" and "Local stream quality" choices, which
    # the selects restore and set; each bridge config write reads them.
    stream_sources: dict[str, StreamSourceChoice] = {}
    lan_qualities: dict[str, LanQualityChoice] = {}
    # Per camera: the configured addresses for the bridge's local stream.
    configured_hosts: dict[str, list[str]] = {}
    write_lock = asyncio.Lock()
    data: dict = {
        "api": api,
        "session": session,
        "coordinators": coordinators,
        "config": entry.data,
        "options": dict(entry.options),
        "stream_sources": stream_sources,
        "lan_qualities": lan_qualities,
    }
    ready = False

    def _live() -> bool:
        # False once setup failed or the entry unloaded: hass.data no longer holds this setup's data.
        return ready and hass.data.get(DOMAIN, {}).get(entry.entry_id) is data

    def _webrtc_only() -> set[str]:
        return {cid for cid, choice in stream_sources.items() if choice.source == STREAM_SOURCE_WEBRTC}

    async def _rewrite_bridge_config(*, refetch: bool = True) -> None:
        # One writer at a time. Its only cloud call, for a P2P password the setup
        # could not fetch, is bounded, so a rewrite during an outage still writes.
        if refetch and _live() and entry.options.get(CONF_LAN_STREAMING, DEFAULT_LAN_STREAMING):
            wanted = [cid for cid in coordinators if cid not in _webrtc_only()]
            passwords.update(await fetch_missing_passwords(api, wanted, passwords))
        async with write_lock:
            # A rewrite queued during the setup write can get the lock only after
            # that setup failed, and must not write for an entry that is gone.
            if not _live():
                return
            lan = lan_credentials(
                coordinators,
                passwords,
                configured_hosts,
                {cid: lan_clarity_for(choice.quality) for cid, choice in lan_qualities.items()},
                {cid: choice.source for cid, choice in stream_sources.items() if choice.source},
                bound_hosts,
            )
            await _write_bridge_config(hass, entry, api, cameras, lan)

    def _on_address_change(_ip: str) -> None:
        # On the event loop. Before `ready` it is dropped: the setup write reads every address.
        # After an unload the entry data is gone, and unload cancels a task already queued.
        if _live():
            entry.async_create_background_task(hass, _rewrite_bridge_config(), "philips_avent_bridge_config")

    def _on_stream_choice(_value) -> None:
        # A camera's local stream quality, or a move to or from "WebRTC only":
        # the bridge config changes, and the add-on restarts the bridge.
        if _live():
            entry.async_create_background_task(hass, _rewrite_bridge_config(), "philips_avent_bridge_config")

    # One per entry: a camera's LAN address is not tried by the others.
    lan_claims = AddressClaims()
    # Each camera's last verified address, claimed before any client connects.
    lan_claims.seed({
        cid: ip
        for cam in cameras
        if (cid := cam.get("deviceId") or cam.get("devId"))
        and isinstance(ip := (cache.get(cid) or {}).get("lan_ip"), str)
    })
    # The option's shared addresses per camera and its "camera=address" bindings.
    shared_hosts, bound_hosts, unbound = lan_addresses(
        hass,
        entry.entry_id,
        entry.options.get(CONF_LAN_HOSTS, DEFAULT_LAN_HOSTS),
        {
            cid: cam.get("deviceName") or cam.get("name") or cid
            for cam in cameras
            if (cid := cam.get("deviceId") or cam.get("devId"))
        },
    )
    if unbound:
        _LOGGER.warning(
            "Camera LAN addresses: %s names no camera of this account, or more than one; ignored",
            ", ".join(unbound),
        )
    # A bound address is its camera's from the start: the others never try it.
    for cid, ip in bound_hosts.items():
        lan_claims.claim(ip, cid)
    skipped: list[str] = []
    try:
        for cam in cameras:
            cam_id = cam.get("deviceId") or cam.get("devId")
            cam_name = cam.get("deviceName") or cam.get("name", cam_id)

            coordinator = PhilipsAventCoordinator(
                hass, api, cam_id, cam_name, local_key=cam.get("localKey"), cache=cache,
                lan_hosts=shared_hosts.get(cam_id, []),
                lan_claims=lan_claims,
                lan_bound=bound_hosts.get(cam_id),
                lan_preferred=lan_preferred,
                account_cloud=account_cloud,
            )
            coordinators[cam_id] = coordinator
            seed = previous.get(cam_id, {})
            try:
                await coordinator.async_start(
                    _on_address_change, seed.get("lan_ip") or None, seed.get("lan_protocol", "")
                )
            except ConfigEntryNotReady as err:
                # One camera the cloud cannot serve now (removed, unshared, a
                # passing error, no cache) must not keep the others away.
                del coordinators[cam_id]
                await coordinator.stop_lan()
                skipped.append(cam_name)
                _LOGGER.warning("%s did not start (%s); the other cameras run, and it is retried", cam_name, err)
        if not coordinators:
            raise ConfigEntryNotReady("no camera could start")

        lan_streaming = entry.options.get(CONF_LAN_STREAMING, DEFAULT_LAN_STREAMING)
        webrtc = entry.options.get(CONF_WEBRTC, DEFAULT_WEBRTC)
        for cam_id, coordinator in coordinators.items():
            seed = previous.get(cam_id, {})
            bound = bound_hosts.get(cam_id)
            configured_hosts[cam_id] = ([bound] if bound else []) + shared_hosts.get(cam_id, [])
            possible = local_stream_possible(lan_streaming, coordinator.local_key, coordinator.lan_protocol, bound)
            # Carried over from the previous bridge config, so the first write
            # matches it; the selects then restore their own choices.
            stream_sources[cam_id] = StreamSourceChoice(
                cam_id, possible, webrtc, seed.get("stream_source"), _on_stream_choice
            )
            if possible:
                lan_qualities[cam_id] = LanQualityChoice(
                    cam_id, resolve_lan_quality(None, seed.get("lan_clarity")), _on_stream_choice
                )

        # With LAN streaming off the bridge file carries no P2P password, so there is nothing to fetch.
        if lan_streaming:
            seeds = {k: v["p2p_password"] for k, v in previous.items()}
            fetch_ids = password_fetch_ids(
                list(coordinators),
                previous,
                {cid: c.device_info.get("localKey") or c.local_key for cid, c in coordinators.items()},
                skip=_webrtc_only(),
                lan_preferred=lan_preferred,
            )
            # Prefer the LAN: a password the previous file has for the same key stays.
            passwords.update({cid: seeds[cid] for cid in coordinators if cid not in fetch_ids and seeds.get(cid)})
            if fetch_ids:
                passwords.update(await fetch_p2p_passwords(api, fetch_ids, seeds))
        hass.data.setdefault(DOMAIN, {})[entry.entry_id] = data
        ready = True
        _bridge_api_token(hass, entry)
        # The passwords were fetched just above; asking again here would only repeat a failure.
        await _rewrite_bridge_config(refetch=False)
        status = await _bridge_status(hass, entry)
        data["bridge_status"] = status
        _connect_lan_share(hass, entry, coordinators, status)
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Setup is retried from scratch, so nothing may outlive this attempt:
        # the LAN clients started here would otherwise run on beside the new ones.
        ready = False
        hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
        for coordinator in coordinators.values():
            await coordinator.stop_lan()
        await session.close()
        raise
    if skipped:
        # A reload tries the others again (it restarts this entry's sessions briefly).
        entry.async_on_unload(async_call_later(
            hass, SKIPPED_CAMERA_RETRY_SECONDS, lambda _now: hass.config_entries.async_schedule_reload(entry.entry_id),
        ))

    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # An entry without stored cameras discovers them on every setup already.
    if stored_cameras:
        entry.async_create_background_task(
            hass, _look_for_new_cameras(hass, entry, api), "philips_avent new cameras"
        )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    if unload_ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        for coordinator in data["coordinators"].values():
            await coordinator.stop_lan()
        await data["session"].close()

    return unload_ok


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Delete this entry's bridge config when the integration is removed.

    Without this the file survived the removal, and the add-on could pick it
    over the file of whatever entry the user created next (issue #52).
    """
    bridge_path = Path(hass.config.path(bridge_config_filename(entry.entry_id)))

    def _unlink() -> bool:
        try:
            bridge_path.unlink()
        except FileNotFoundError:
            return False
        except OSError as err:
            _LOGGER.warning("Could not remove bridge config %s: %s", bridge_path, err)
            return False
        return True

    if await hass.async_add_executor_job(_unlink):
        _LOGGER.info("Removed bridge config %s", bridge_path)

    # The offline cache holds the cameras' local keys; it goes with the entry.
    await offline_cache(hass, entry.entry_id).async_remove()
