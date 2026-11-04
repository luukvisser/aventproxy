"""Data update coordinator for Philips Avent."""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util.dt import now as dt_now
from homeassistant.util.dt import utcnow

from . import senseiq
from .api import PhilipsAventAPI, TuyaAPIError
from .cloud_policy import (
    DEVICE_INFO_REFRESH_INTERVAL,
    FAILURE_API,
    FAILURE_AUTH,
    FAILURE_UNREACHABLE,
    KEY_REFRESH_INTERVAL,
    LAN_FALLBACK_DELAY,
    LAN_RESYNC_INTERVAL,
    LAN_STABLE_DELAY,
    START_LAN_ONLY,
    UNREACHABLE_ERRORS,
    AccountCloud,
    CloudState,
    cloud_reports_offline,
    cloud_retry_seconds,
    is_auth_error,
    keep_serving_without_cloud,
    refresh_allowed,
    startup_mode,
)
from .const import (
    DPS_ALARM_RECORD,
    DPS_LULLABY_CONTROL,
    DPS_LULLABY_STATE,
    DPS_SENSEIQ_STATUS,
    mirror_dps_to_cloud,
)
from .events import LULLABY_SETTLE_SECONDS, lullaby_state_settled, poll_should_stay_fast
from .lan import TuyaLANClient
from .lan_policy import AddressClaims, keyed_protocol, lan_command_failure_message, parse_protocol_version
from .offline_cache import (
    build_entry,
    describe,
    device_info_from_entry,
    restorable_dps,
    usable_for_fallback,
    usable_for_lan_preferred,
)
from .payload import dps_delta, truncated_dps

LULLABY_STATE_MAP = {"play": "playing", "pause": "stopping", "stop": "stopping"}

_LOGGER = logging.getLogger(__name__)

POLL_FAST = timedelta(seconds=30)
POLL_SLOW = timedelta(seconds=120)

# RSSI moves slowly and costs a second API call per poll, so it is refreshed on
# its own schedule rather than on every tick of a fast poll.
RSSI_INTERVAL = timedelta(minutes=5)

# The SenseIQ daily sleep summary is a cloud aggregate that changes slowly, so it
# is fetched on its own schedule rather than on every DPS poll.
SLEEP_DAY_INTERVAL = timedelta(minutes=15)


# What a cloud call can fail with, once Tuya errors are mapped to UpdateFailed or
# ConfigEntryAuthFailed. A load balancer's HTML error page is a JSON ValueError.
CLOUD_FAILURES = (UpdateFailed, ConfigEntryAuthFailed, aiohttp.ClientError, TimeoutError, ValueError)


def _session_rejected(err: TuyaAPIError) -> bool:
    """Whether the cloud refused the stored session, which only a reauth fixes."""
    return "SID_INVALID" in str(err) or "USER_SESSION_LOSS" in str(err)


class PhilipsAventCoordinator(DataUpdateCoordinator):
    """Polls camera DPS values, with optional LAN push for real-time updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        api: PhilipsAventAPI,
        camera_id: str,
        camera_name: str,
        local_key: str | None = None,
        lan_hosts: str | list[str] | None = None,
        lan_claims: AddressClaims | None = None,
        lan_bound: str | None = None,
        cache=None,
        lan_preferred: bool = False,
        account_cloud: AccountCloud | None = None,
    ):
        super().__init__(
            hass,
            _LOGGER,
            name=f"Philips Avent {camera_name}",
            update_interval=POLL_FAST,
        )
        self.api = api
        self.camera_id = camera_id
        self.camera_name = camera_name
        self.device_info: dict = {}
        self.rssi: int | None = None
        self._local_key = local_key
        self._lan_client: TuyaLANClient | None = None
        self._lan_hosts = lan_hosts
        self._lan_claims = lan_claims
        # The address the options bind to this camera (lan_policy.parse_lan_hosts).
        self._lan_bound = lan_bound
        # The last published LAN address and protocol from the bridge config,
        # which upstream dials before a scan (lan_policy.seeded_session).
        self._lan_seed: tuple[str | None, str] = (None, "")
        # Rewrites the bridge config; the same callback the LAN client calls on a new address.
        self._rewrite_bridge_config: Callable[[str], None] | None = None
        self.last_lan_dps: dict[str, Any] = {}
        self.lan_update_sequence = 0
        self._pending_lullaby: str | None = None
        self._pending_lullaby_since: float | None = None
        self._lullaby_unsub = None
        # Monotonic time of the last DPS 3 (SenseIQ status) LAN push, and the
        # timer that re-evaluates entities once it would go stale.
        self._senseiq_pushed_at: float | None = None
        self._senseiq_stale_unsub = None
        self._rssi_refreshed_at = None
        # SenseIQ nightly sleep summary (cloud aggregate), decoded in senseiq.py.
        self.sleep_day: dict | None = None
        self._sleep_day_refreshed_at = None
        self._cloud_poll_paused = False
        self._reauth_requested = False
        # Offline start (see cloud_policy.py): the per-entry cache, the cloud's
        # state, and the LAN address and protocol to try before a scan.
        self._cache = cache
        self._cloud = CloudState(account_cloud)
        if account_cloud is not None:
            # Another camera's contact changes what Cloud Connection shows here.
            account_cloud.add_listener(self._account_cloud_changed)
        self._lan_only_logged = False
        self._lan_ip: str | None = None
        self._lan_version: float | None = None
        # LAN preferred (CONF_LAN_PREFERRED): the cloud only when the cache
        # cannot do the job; the monotonic time of the last key refresh rate
        # limits the one case the LAN itself asks for it.
        self._lan_preferred = lan_preferred
        self._key_refreshed_at: float | None = None
        self._device_refresh_unsub = None
        # The cloud poll standing in while the LAN is down in LAN preferred.
        self._cloud_fallback = False
        self._cloud_fallback_unsub = None
        # Pending while a LAN that came back has not stayed up LAN_STABLE_DELAY.
        self._lan_stable_unsub = None
        # Whether a LAN session ever came up (a later connect is a reconnect),
        # and when the last resync after one ran.
        self._lan_was_up = False
        self._resynced_at: float | None = None
        # Logged once per period that the cloud reports the camera offline,
        # and whether it already did so on the first fetch.
        self._offline_logged = False
        self._offline_at_start = False
        # Set by stop_lan: nothing may start a LAN client or write the cache
        # after an unload.
        self._stopped = False


    @property
    def lan_preferred(self) -> bool:
        return self._lan_preferred

    @property
    def cloud_available(self) -> bool:
        """Whether the cloud serves this camera: the last fetch succeeded, outside LAN preferred."""
        return not self._lan_preferred and self._cloud.available is True

    @property
    def _cloud_view(self):
        return self._cloud.account or self._cloud

    @property
    def _cloud_may_be_up(self) -> bool:
        """Whether a command may be mirrored to the cloud: not LAN preferred, and not known to be down."""
        return not self._lan_preferred and self._cloud_view.available is not False

    @property
    def cloud_reachable(self) -> bool | None:
        """Whether the last contact with the cloud worked, in any mode; None before the first.

        The account's last contact when the entry shares one (any camera's),
        else this camera's. In LAN preferred contacts are the start, the daily
        device refresh, a key refresh, a resync after a LAN drop and the
        fallback poll.
        """
        return self._cloud_view.available

    @property
    def cloud_checked_at(self) -> float | None:
        """When the last cloud contact was (epoch seconds), or None."""
        return self._cloud_view.checked_at

    @property
    def camera_online(self) -> bool | None:
        """Whether the camera itself is connected to Tuya, as the cloud last said; None if not known.

        From isOnline in the device record of the last fetch. The offline
        cache does not keep it, so after a start from the cache it is None
        until the next fetch for this camera.
        """
        value = (self.device_info or {}).get("isOnline")
        return value if isinstance(value, bool) else None

    @callback
    def _account_cloud_changed(self) -> None:
        if not self._stopped and self.data is not None:
            self.async_update_listeners()

    @property
    def cloud_failure(self) -> str | None:
        """Why the last cloud fetch failed (cloud_policy.FAILURE_*), or None."""
        return self._cloud.last_failure

    async def async_start(
        self,
        on_address_change: Callable[[str], None] | None = None,
        last_ip: str | None = None,
        last_protocol: str = "",
    ) -> None:
        """First refresh from the cloud, or a start on the LAN from the cache, then the LAN session.

        `on_address_change` rewrites the bridge config; `last_ip` and
        `last_protocol` are the pair it last published (start_lan).

        A cloud that cannot be reached used to leave the entry in setup-retry
        although the camera answered on the LAN. With a cached key the camera
        now starts on the LAN instead and the poll keeps asking the cloud, with
        backoff, until it answers. An auth error still raises
        ConfigEntryAuthFailed, and without a cache the ConfigEntryNotReady stands.
        Video is not covered: the bridge reaches the camera through Tuya's MQTT
        and still needs the cloud.
        """
        self._rewrite_bridge_config = on_address_change
        self._lan_seed = (last_ip, last_protocol)
        cached = self._cache.get(self.camera_id) if self._cache else None
        if cached:
            self._lan_ip = cached.get("lan_ip")
            self._lan_version = cached.get("lan_version")
        if self._lan_preferred and usable_for_lan_preferred(cached):
            self._start_from_cache(cached)
            _LOGGER.info(
                "%s starts on the LAN from the cache (%s); LAN preferred, so the cloud is not polled",
                self.camera_name, describe(cached),
            )
        else:
            await self._async_start_from_cloud(cached)
            if self._offline_at_start:
                # The first fetch had to succeed, or the setup of every camera
                # of the entry fails with it; the camera is unavailable now
                # rather than at the next poll (which in LAN preferred is the
                # fallback, five minutes away). The LAN coming up makes it
                # available, as any LAN session does.
                self.last_update_success = False
        if self._lan_preferred:
            self._start_lan_preferred()
        await self.start_lan()

    async def _async_start_from_cloud(self, cached: dict | None) -> None:
        try:
            await self.async_config_entry_first_refresh()
        except ConfigEntryNotReady:
            if startup_mode(self.cloud_failure, usable_for_fallback(cached)) != START_LAN_ONLY:
                raise
            self._start_from_cache(cached)
            self._lan_only_logged = True
            _LOGGER.warning(
                "Tuya cloud unreachable at startup; %s runs on the LAN with the cached key (%s) "
                "until it returns. Video, WiFi signal and the SenseIQ sleep summary need the cloud",
                self.camera_name, describe(cached),
            )

    def _start_lan_preferred(self) -> None:
        """No cloud poll from here on; only the daily device refresh and a refused key ask.

        What this costs, and why the option is off by default:
        - alerts the camera reports only through the cloud: sound on an SCD951
          arrived only through the poll, motion arrives locally on 3.4 and up;
        - settings changed in the Philips app, which reached Home Assistant only
          through the cloud poll (sensitivity, measured 01-11-2026);
        - the WiFi signal and the SenseIQ sleep summary, which are cloud calls;
        - video, since the bridge still gets it through Tuya's MQTT.
        """
        self.update_interval = None
        self._lan_only_logged = True
        self._schedule_cloud_fallback()
        self._device_refresh_unsub = async_track_time_interval(
            self.hass, self._scheduled_device_refresh, timedelta(seconds=DEVICE_INFO_REFRESH_INTERVAL)
        )

    def _start_from_cache(self, entry: dict) -> None:
        """Take key, device details and the last state from the cache.

        Entities stay unavailable until the LAN session is up
        (_on_lan_connection); the values they then show are the cached ones
        until the camera pushes a change, except the VOLATILE_DPS, which stay
        unknown until then rather than show a lullaby or alert from the past.
        """
        self._local_key = entry["localKey"]
        self.device_info = device_info_from_entry(entry)
        self.data = restorable_dps(entry)
        self.last_update_success = False

    async def start_lan(self) -> None:
        # Entries store no key, so it usually comes from the first refresh.
        if not self._local_key:
            self._local_key = self.device_info.get("localKey")
        if not self._local_key or self._stopped:
            return
        last_ip, last_protocol = self._lan_seed
        self._lan_client = TuyaLANClient(
            self.hass,
            self.camera_id,
            self._local_key,
            self._on_lan_dps_update,
            self._rewrite_bridge_config,
            last_ip,
            last_protocol,
            on_session_up=self._mark_lan_live,
            hosts=self._lan_hosts,
            claims=self._lan_claims,
            bound=self._lan_bound,
            ip=self._lan_ip,
            version=self._lan_version,
            on_connection_change=self._on_lan_connection,
            on_key_rejected=self._on_lan_key_rejected,
        )
        await self._lan_client.start()

    async def stop_lan(self) -> None:
        self._stopped = True
        self._cancel_pending_lullaby()
        if self._cloud_fallback_unsub:
            self._cloud_fallback_unsub()
            self._cloud_fallback_unsub = None
        if self._lan_stable_unsub:
            self._lan_stable_unsub()
            self._lan_stable_unsub = None
        if self._device_refresh_unsub:
            self._device_refresh_unsub()
            self._device_refresh_unsub = None
        if self._senseiq_stale_unsub:
            self._senseiq_stale_unsub()
            self._senseiq_stale_unsub = None
        if self._lan_client:
            await self._lan_client.stop()
            self._lan_client = None

    @property
    def local_key(self) -> str | None:
        """The key the LAN session uses: the one it started with, or the one the cloud rotated to."""
        return self._local_key

    @property
    def lan_ip(self) -> str | None:
        return self._lan_client.ip if self._lan_client else None

    @property
    def lan_protocol(self) -> str:
        return self._lan_client.protocol if self._lan_client else ""

    @property
    def lan_connected(self) -> bool:
        return self._lan_client is not None and self._lan_client.connected

    @property
    def lan_live(self) -> bool:
        """Whether a LAN session is up and has proven its key (TuyaLANClient.proven).

        This, not a bare connect, is what keeps the entities available through a
        cloud failure.
        """
        return self._lan_client is not None and self._lan_client.proven

    @callback
    def _on_lan_connection(self, connected: bool) -> None:
        """Keep the LAN address for the cache, and availability while the cloud is away.

        With the cloud answering, availability follows the poll as it always
        has. Without it the LAN session is the only source, so entities are
        available exactly while it is up.
        """
        if connected and self._lan_client:
            self._note_lan_address()
            if self._lan_preferred and not self.lan_alerts_local and self.update_interval is None:
                # A 3.3 session: alerts still come from the cloud poll. Setting
                # the interval alone schedules nothing when no poll is pending.
                self.update_interval = POLL_FAST
                self.hass.async_create_task(self.async_request_refresh())
        if self._lan_preferred:
            if connected:
                self._schedule_lan_stable()
                if self._lan_was_up:
                    self._resync_after_reconnect()
            else:
                self._cancel_lan_stable()
                self._schedule_cloud_fallback()
        if connected:
            self._lan_was_up = True
        if self.cloud_available or self.data is None:
            # The LAN connection sensor follows the session in every mode.
            self.async_update_listeners()
            return
        if connected and not self._lan_only_logged and not self._lan_preferred:
            self._lan_only_logged = True
            _LOGGER.warning("Tuya cloud unreachable; %s runs on the LAN until it returns", self.camera_name)
        if connected:
            # Only a session that proved it is this camera keeps the entities
            # available (_mark_lan_live); a 3.3 one does so at its first
            # readable message (_show_lan_state).
            self._mark_lan_live(notify=False)
        else:
            self.last_update_success = False
        self.async_update_listeners()

    @callback
    def _note_lan_address(self) -> None:
        """Keep the session's address and protocol for the cache, once it is this camera's.

        A 3.3 session proves that with its first readable message, not its
        handshake: until then it may be another device's, whose address would
        otherwise be tried first at every start.
        """
        client = self._lan_client
        if not (client and client.verified):
            return
        if (client.session_ip, client.version) == (self._lan_ip, self._lan_version):
            return
        self._lan_ip, self._lan_version = client.session_ip, client.version
        if self._cache:
            self.hass.async_create_task(self._async_update_cache(self.data))

    @callback
    def _schedule_cloud_fallback(self) -> None:
        """In LAN preferred: poll the cloud if the LAN is still down after LAN_FALLBACK_DELAY."""
        if self._cloud_fallback or self._cloud_fallback_unsub or self._stopped:
            return
        self._cloud_fallback_unsub = async_call_later(self.hass, LAN_FALLBACK_DELAY, self._start_cloud_fallback)

    @callback
    def _schedule_lan_stable(self) -> None:
        """End the cloud fallback once the LAN has stayed up LAN_STABLE_DELAY."""
        self._cancel_lan_stable()
        if not self._stopped:
            self._lan_stable_unsub = async_call_later(self.hass, LAN_STABLE_DELAY, self._lan_stable)

    @callback
    def _cancel_lan_stable(self) -> None:
        if self._lan_stable_unsub:
            self._lan_stable_unsub()
            self._lan_stable_unsub = None

    @callback
    def _lan_stable(self, _now=None) -> None:
        self._lan_stable_unsub = None
        if not self._stopped and self.lan_connected:
            self._end_cloud_fallback()

    @callback
    def _resync_after_reconnect(self) -> None:
        """One cloud fetch after a LAN drop, at most every LAN_RESYNC_INTERVAL."""
        now = time.monotonic()
        if self._stopped or not refresh_allowed(self._resynced_at, now, LAN_RESYNC_INTERVAL):
            return
        self._resynced_at = now
        self.hass.async_create_task(self._async_resync())

    async def _async_resync(self) -> None:
        try:
            data = await self._fetch_from_cloud()
        except (TuyaAPIError, *UNREACHABLE_ERRORS) as err:
            _LOGGER.debug("%s: no cloud resync after the LAN came back (%s)", self.camera_name, err)
            self._cloud.failed(FAILURE_API if isinstance(err, TuyaAPIError) else FAILURE_UNREACHABLE)
            self.async_update_listeners()
            return
        self._cloud.succeeded()
        if self._stopped:
            return
        _LOGGER.debug("%s: state resynced from the cloud after the LAN came back", self.camera_name)
        self.async_set_updated_data(data)
        await self._async_update_cache(data)

    @callback
    def _start_cloud_fallback(self, _now=None) -> None:
        self._cloud_fallback_unsub = None
        # A LAN that is up but not yet stable does not stop the fallback: one
        # that flaps would otherwise keep it away for good.
        if self._stopped or (self.lan_connected and self._lan_stable_unsub is None):
            return
        self._cloud_fallback = True
        _LOGGER.warning(
            "%s has not been on the LAN for %d minutes; polling the Tuya cloud until it is back",
            self.camera_name, LAN_FALLBACK_DELAY // 60,
        )
        self.update_interval = POLL_SLOW
        self.hass.async_create_task(self.async_request_refresh())

    @callback
    def _end_cloud_fallback(self) -> None:
        if self._cloud_fallback_unsub:
            self._cloud_fallback_unsub()
            self._cloud_fallback_unsub = None
        if self._cloud_fallback:
            self._cloud_fallback = False
            if self._cloud_poll_off:
                self.update_interval = None
                _LOGGER.info("%s is back on the LAN; the cloud poll stops (LAN preferred)", self.camera_name)
            else:
                self.update_interval = POLL_FAST
                _LOGGER.info(
                    "%s is back on the LAN on protocol %s; the cloud poll stays for its alerts",
                    self.camera_name, self._lan_client.version if self._lan_client else self._lan_version,
                )

    @property
    def lan_alerts_local(self) -> bool:
        """Whether alerts can be expected over this camera's LAN session.

        Not on protocol 3.3: there the camera never pushed its alarm data
        points (const.py, DPS_ALARM_RECORD), so a 3.3 camera keeps the cloud
        poll for its alerts even in LAN preferred, while status and commands
        go over the LAN. Unknown (no session yet) counts as local. The running
        session's protocol counts before it proved it is this camera's.
        """
        version = self._lan_client.version if self.lan_connected else self._lan_version
        return version is None or keyed_protocol(version)

    @property
    def _cloud_poll_off(self) -> bool:
        """LAN preferred with the LAN doing its job: no scheduled cloud poll."""
        return self._lan_preferred and not self._cloud_fallback and self.lan_alerts_local

    @callback
    def _on_lan_key_rejected(self) -> None:
        """The camera refused the cached key: in LAN preferred, ask the cloud for the current one.

        Outside LAN preferred the poll already brings a changed key. Rate
        limited by KEY_REFRESH_INTERVAL, since a refusal can also mean a wrong
        protocol version, which a cloud refresh does not fix.
        """
        if not self._lan_preferred:
            return
        now = time.monotonic()
        if not refresh_allowed(self._key_refreshed_at, now, KEY_REFRESH_INTERVAL):
            _LOGGER.debug("%s refused the local key; a cloud refresh ran within the hour", self.camera_name)
            return
        self._key_refreshed_at = now
        self.hass.async_create_task(
            self._async_refresh_device("the camera refused the cached local key")
        )

    @callback
    def _scheduled_device_refresh(self, _now=None) -> None:
        self.hass.async_create_task(self._async_refresh_device("daily refresh of the device details"))

    async def _async_refresh_device(self, reason: str) -> None:
        """One cloud fetch of the device record and key in LAN preferred, then back to the LAN.

        A failure leaves everything as it was: the LAN carries on with what it
        has, and the next refusal or the next day asks again.
        """
        _LOGGER.info("Asking the Tuya cloud for the device record of %s: %s", self.camera_name, reason)
        try:
            device = await self.api.get_device(self.camera_id)
        except TuyaAPIError as err:
            if is_auth_error(err):
                _LOGGER.warning(
                    "Tuya session for %s is no longer valid (%s); asking to log in again "
                    "(the LAN keeps running meanwhile)", self.camera_name, err,
                )
                # Without this, LAN preferred lost WebRTC and every cloud-only
                # part silently: the reauth flow only notifies, nothing stops.
                if self.config_entry is not None:
                    self.config_entry.async_start_reauth(self.hass)
            else:
                _LOGGER.warning("Tuya cloud refused the device record for %s: %s", self.camera_name, err)
            self._cloud.failed(FAILURE_AUTH if is_auth_error(err) else FAILURE_API)
            self.async_update_listeners()
            return
        except UNREACHABLE_ERRORS as err:
            _LOGGER.warning("Tuya cloud unreachable for %s (%s); staying on the cached key", self.camera_name, err)
            self._cloud.failed(FAILURE_UNREACHABLE)
            self.async_update_listeners()
            return
        if not isinstance(device, dict):
            _LOGGER.warning("Tuya cloud returned no device record for %s; keeping the last one", self.camera_name)
            return
        self._cloud.succeeded()
        self.async_update_listeners()
        self.device_info = device
        self._adopt_local_key(device.get("localKey"))
        await self._async_update_cache(self.data)

    async def _async_update_cache(self, dps: dict | None) -> None:
        if not self._cache or self._stopped:
            return
        entry = build_entry(self._local_key, self.device_info, dps, self._lan_ip, self._lan_version)
        await self._cache.async_update(self.camera_id, entry)

    @property
    def lan_serves_state(self) -> bool:
        """Whether the live LAN session negotiated 3.4 or up (see lan_policy.keyed_protocol).

        Such a session answers the baseline query and pushes alarms in DPS 212.
        On its own that does not take the cloud out of the loop:
        `cloud_poll_pausable` also needs DPS 212 in the monitor's state, and
        both the paused poll and the end of the cloud mirror follow it. On 3.3
        the cloud stays in the loop exactly as before.
        """
        return self._lan_client is not None and keyed_protocol(self._lan_client.session_version)

    @property
    def alert_detectors_exclusive(self) -> bool:
        """Whether turning one alert detector on has to turn the other off.

        Measured only on a monitor speaking 3.5 (fork 04d6d28), so it applies to
        a monitor whose LAN session confirmed 3.4 or up. That protocol is the one
        published to the bridge, which survives restarts and blips, so the
        switches behave the same whether or not the session is up right now. A
        3.3 monitor or a cloud-only setup keeps them independent, as before.
        """
        return keyed_protocol(parse_protocol_version(self.lan_protocol))

    @property
    def senseiq_status_stale(self) -> bool:
        """Whether the SenseIQ live status has stopped arriving; see senseiq.status_is_stale."""
        return senseiq.status_is_stale(self.lan_connected, self._senseiq_pushed_at, time.monotonic())

    @callback
    def _note_senseiq_push(self) -> None:
        """Record a DPS 3 push and re-check the entities once it would go stale.

        Without the timer nothing would re-evaluate Baby Detected after the
        pushes stop, until some unrelated update or the next poll arrived.
        """
        self._senseiq_pushed_at = time.monotonic()
        if self._senseiq_stale_unsub:
            self._senseiq_stale_unsub()
        self._senseiq_stale_unsub = async_call_later(
            self.hass, senseiq.STATUS_STALE_AFTER_SECONDS + 1, self._senseiq_went_stale
        )

    @callback
    def _senseiq_went_stale(self, _now=None) -> None:
        self._senseiq_stale_unsub = None
        _LOGGER.debug(
            "SenseIQ status for %s went stale after %ss without a push",
            self.camera_name, senseiq.STATUS_STALE_AFTER_SECONDS,
        )
        self.async_update_listeners()

    @callback
    def _on_lan_dps_update(self, dps: dict[str, Any], baseline: bool = False) -> None:
        """Merge DPS the LAN session reported into entity state.

        A baseline is the answer to the query a 3.4+ session sends when it comes
        up, so it is state and not news: counting it as a push would let a
        retained alert value fire the motion or sound sensor on every reconnect.
        A SenseIQ status in it still starts the staleness clock, so a retained
        "baby present" cannot stand forever if no live push follows.
        """
        if self.data is None:
            return
        if not baseline:
            self.last_lan_dps = dict(dps)
            self.lan_update_sequence += 1
        self._note_lan_address()
        _LOGGER.debug(
            "LAN %s for %s: %s",
            "baseline" if baseline else "push", self.camera_name, truncated_dps(dps),
        )

        if DPS_SENSEIQ_STATUS in dps:
            self._note_senseiq_push()
        dps = self._hold_lullaby_state(dps)
        self._show_lan_state({**self.data, **dps})

    @callback
    def _show_lan_state(self, data: dict[str, Any]) -> None:
        """Show state the LAN session reported, leaving the cloud poll where it is.

        `async_set_updated_data` also reschedules the next refresh a full
        interval out, so pushes arriving faster than the poll interval (a
        SenseIQ status every few seconds) postponed the poll indefinitely. On a
        3.3 session, where alarms arrive only through the poll, they stopped
        arriving. A push does end a failed poll's verdict, through
        `_mark_lan_live`: the session it came on keeps the entities available,
        and a poll does not fail while that session is up.
        """
        self.data = data
        self._mark_lan_live(notify=False)
        self.async_update_listeners()

    @callback
    def _mark_lan_live(self, notify: bool = True) -> None:
        """A LAN session is live, so the entities are available whatever the cloud says.

        Undoes a failed poll's verdict as soon as a session comes up or a push
        arrives, without waiting for the next tick. While the session stays up a
        failing poll no longer raises (`_without_cloud`), so this cannot
        flap. Only a session that proved its key counts, so neither a bare 3.3
        connect nor a lullaby state settling after the session dropped can do it.
        """
        if self.data is None or self.last_update_success or not self.lan_live:
            return
        self.last_update_success = True
        if notify:
            self.async_update_listeners()

    @callback
    def _hold_lullaby_state(self, dps: dict[str, Any]) -> dict[str, Any]:
        """Keep a lullaby state out of entity state until it stands still.

        The camera re-announces `stopping` and then `playing` within a third of a
        second when a viewing session ends, which reached the Lullaby Playing
        sensor as a flicker (issue #72). Holding the value lets such a pair
        cancel itself.
        """
        if DPS_LULLABY_STATE not in dps:
            return dps

        value = dps[DPS_LULLABY_STATE]
        rest = {k: v for k, v in dps.items() if k != DPS_LULLABY_STATE}

        if value == (self.data or {}).get(DPS_LULLABY_STATE):
            # Already the state on show; nothing to hold or cancel.
            self._cancel_pending_lullaby()
            return rest

        self._pending_lullaby = value
        self._pending_lullaby_since = time.monotonic()
        if self._lullaby_unsub:
            self._lullaby_unsub()
        self._lullaby_unsub = async_call_later(
            self.hass, LULLABY_SETTLE_SECONDS, self._commit_lullaby_state
        )
        _LOGGER.debug(
            "Holding lullaby state %r for %s until it settles",
            value, self.camera_name,
        )
        return rest

    @callback
    def _cancel_pending_lullaby(self) -> None:
        if self._lullaby_unsub:
            self._lullaby_unsub()
            self._lullaby_unsub = None
        self._pending_lullaby = None
        self._pending_lullaby_since = None

    @callback
    def _commit_lullaby_state(self, _now=None) -> None:
        """Apply a held lullaby state once it has stood still."""
        self._lullaby_unsub = None
        if not lullaby_state_settled(
            self._pending_lullaby, self._pending_lullaby_since, time.monotonic()
        ):
            return
        value = self._pending_lullaby
        self._pending_lullaby = None
        self._pending_lullaby_since = None
        if self.data is None or value is None:
            return
        _LOGGER.debug("Lullaby state settled at %r for %s", value, self.camera_name)
        self._show_lan_state({**self.data, DPS_LULLABY_STATE: value})

    @callback
    def _apply_optimistic(self, dps: dict, extra: dict | None = None) -> None:
        """Show a command's effect before the monitor confirms it.

        Always a new dict: entities never write into `data` themselves.
        """
        if self.data is None:
            return
        optimistic = {str(k): v for k, v in dps.items()}
        lullaby_cmd = optimistic.get(DPS_LULLABY_CONTROL)
        if lullaby_cmd in LULLABY_STATE_MAP:
            optimistic[DPS_LULLABY_STATE] = LULLABY_STATE_MAP[lullaby_cmd]
        if extra:
            optimistic.update(extra)
        # Not async_set_updated_data: that also plans the next poll a whole
        # interval out, so commands in quick succession (a volume slider)
        # postponed the poll and the alerts only it brings (see _show_lan_state).
        self.data = {**self.data, **optimistic}
        self.async_update_listeners()

    async def set_dps(self, dps: dict, *, optimistic: dict | None = None) -> dict:
        """Send a DPS command, over the LAN when there is a session for it.

        While the cloud poll is paused behind a 3.4+ session
        (`cloud_poll_pausable`), a command the LAN delivered is done: the monitor
        reports the change on that same session, so repeating it through the
        cloud only sends the write over the internet again (fork a59b77f).
        Whenever the cloud is still polled, a LAN success is mirrored to it as
        before, except relative lullaby steps (`mirror_dps_to_cloud`), so a poll
        cannot hand back a cloud state that never heard of the change. A command
        the LAN could not deliver goes through the cloud on any protocol.

        `optimistic` carries values the monitor reports back under another key
        than the one written, such as the lullaby track in DPS 248.

        A camera running without the cloud skips the mirror, which would only
        wait for the timeout. In LAN preferred a command that the LAN cannot
        deliver fails instead of using the cloud, except while the cloud poll
        stands in for a LAN that stays down.
        """
        local_only = self.cloud_poll_pausable
        lan = self._lan_client
        if lan and lan.connected:
            repeatable = mirror_dps_to_cloud(dps)
            result = await lan.set_dps(dps, repeatable=repeatable)
            if result:
                _LOGGER.debug("DPS sent via LAN for %s: %s", self.camera_name, dps)
                self._apply_optimistic(dps, optimistic)
                if not local_only and repeatable and self._cloud_may_be_up:
                    with contextlib.suppress(TuyaAPIError, *UNREACHABLE_ERRORS):
                        await self.api.set_dps(self.camera_id, dps)
                return result
        if self._cloud_poll_off:
            raise HomeAssistantError(lan_command_failure_message(
                self.camera_name,
                bool(lan and lan.connected),
                getattr(lan, "last_command_failure", None),
            ))
        self._apply_optimistic(dps, optimistic)
        return await self.api.set_dps(self.camera_id, dps)

    async def _fetch_rssi(self) -> bool:
        """Refresh the WiFi signal if it is due; True when the cloud answered.

        At most once per RSSI_INTERVAL. Behind a paused poll this is the one
        regular cloud call left, so it is what tells the cloud is gone, back, or
        refusing the session. It raises for a transport failure (one of
        CLOUD_FAILURES) and for a refused session (ConfigEntryAuthFailed). Any
        other Tuya error is an answer, from a cloud that is up: logged at debug
        and counted as answered, so an endpoint a model does not serve cannot
        pass for an outage.
        """
        now = utcnow()
        if self._rssi_refreshed_at and now - self._rssi_refreshed_at < RSSI_INTERVAL:
            return False
        self._rssi_refreshed_at = now
        try:
            rssi_data = await self.api.get_rssi(self.camera_id)
        except TuyaAPIError as err:
            if _session_rejected(err):
                raise ConfigEntryAuthFailed(f"Authentication failed: {err}") from err
            _LOGGER.debug("WiFi signal refresh for %s refused by the cloud: %s", self.camera_name, err)
            return True
        if isinstance(rssi_data, dict):
            self.rssi = rssi_data.get("value")
        return True

    async def _refresh_rssi(self) -> None:
        """Refresh the WiFi signal alongside the DPS poll, which a failure must not fail."""
        try:
            await self._fetch_rssi()
        except ConfigEntryAuthFailed:
            raise
        except CLOUD_FAILURES as err:
            _LOGGER.debug("WiFi signal refresh failed for %s: %s", self.camera_name, err)

    async def _refresh_sleep_day(self) -> None:
        """Fetch and decode the SenseIQ nightly sleep summary, throttled.

        Self-contained: a failure here (e.g. a monitor without SenseIQ, or the
        cloud endpoint refusing the call) never disturbs the DPS poll.
        """
        now = utcnow()
        if (
            self._sleep_day_refreshed_at
            and now - self._sleep_day_refreshed_at < SLEEP_DAY_INTERVAL
        ):
            return
        # Stamped before the request, so a server that keeps refusing the call
        # is asked again after SLEEP_DAY_INTERVAL rather than on every poll.
        self._sleep_day_refreshed_at = now
        date = dt_now().strftime("%Y%m%d")
        try:
            result = await self.api.get_sleep_day(self.camera_id, date)
        except (TuyaAPIError, aiohttp.ClientError, TimeoutError, ValueError) as err:
            _LOGGER.warning(
                "SenseIQ sleep-day fetch failed for %s: %s", self.camera_name, err
            )
            return
        self.sleep_day = senseiq.decode_sleep_day(result, date)
        _LOGGER.debug(
            "SenseIQ sleep-day for %s (%s): %s",
            self.camera_name, date, self.sleep_day,
        )

    @property
    def alerts_need_the_cloud(self) -> bool:
        """Whether alerts on this monitor are only visible on the cloud poll.

        Monitors reporting alarms in DPS 212 (the SCD951 and SCD953 on #42 and
        #61, and an SCD973/26 in the fork) depend on the negotiated protocol for
        it: 212 never arrived on 3.3, while from 3.4 up it pushes within a couple
        of seconds (#61 rc9, fork 619981b), which is why a 3.4+ session stops the
        cloud poll altogether (`cloud_poll_pausable`). DPS 212 is also a single slot
        holding the newest alarm rather than a queue, so a second alert
        overwrites the first and whatever the poll misses is gone. Below 3.4 the
        fast poll therefore stays for these monitors as the floor.
        """
        return DPS_ALARM_RECORD in (self.data or {})

    @property
    def cloud_poll_pausable(self) -> bool:
        """Whether the cloud poll can pause: a 3.4+ session carries state and this monitor's alarms.

        Alarms push over a 3.4+ session in DPS 212 (fork 619981b). A monitor that
        has never reported 212 may raise its alerts in 250 and 141 instead, as
        the SCD973 on 3.3 does, and nothing shows those pushing on 3.4+: it keeps
        polling until 212 turns up. A 212 monitor polls on the 30 s tick
        (`poll_should_stay_fast`), so a dropped session resumes the poll within
        30 s.
        """
        return self.data is not None and self.lan_serves_state and self.alerts_need_the_cloud

    @property
    def alerts_heard(self) -> bool:
        """Whether an alert raised now would reach the motion, sound and crying sensors.

        Below 3.4, and on a 3.4+ monitor that has not reported DPS 212, alerts
        arrive only through the cloud poll. While a LAN session covers a failing
        poll everything the session pushes stays usable, but those sensors would
        sit at "off" while deaf: a cry alert that looks healthy. They read
        unavailable instead until the cloud answers again. Behind a paused 3.4+
        poll alerts push over the LAN, so they stay available.
        """
        return self.cloud_poll_pausable or self.cloud_reachable is not False

    @callback
    def _adopt_local_key(self, key: str | None) -> None:
        """Take a local key the cloud rotated, for the LAN session and the bridge.

        The key changes when the monitor is paired again. The LAN client kept the
        one it was started with for the whole run, so it could not reconnect,
        and the bridge config kept the old one too, until Home Assistant
        restarted. The bridge config reads the cloud's key, so a rewrite is
        enough there.
        """
        if not key or key == self._local_key:
            return
        self._local_key = key
        if self._lan_client is None:
            return
        _LOGGER.info("Local key for %s changed in the cloud, handing it to the LAN session", self.camera_name)
        self._lan_client.set_local_key(key)
        if self._rewrite_bridge_config:
            self._rewrite_bridge_config(self.lan_ip or "")

    @callback
    def _note_cloud_poll(self, paused: bool) -> None:
        """Log when the cloud poll stops or starts again behind a LAN session."""
        if paused == self._cloud_poll_paused:
            return
        self._cloud_poll_paused = paused
        if paused:
            _LOGGER.info(
                "Cloud poll for %s paused: its LAN session at protocol %s pushes state and alarms",
                self.camera_name, self._lan_client.session_version if self._lan_client else None,
            )
        else:
            _LOGGER.info("Cloud poll for %s resumed", self.camera_name)

    async def _async_update_data(self) -> dict:
        if self._cloud_poll_off and self.data is not None:
            # No cloud poll in LAN preferred; a manual refresh (update_entity)
            # lands here and reports the LAN state.
            if self.lan_live:
                return self.data
            raise UpdateFailed(f"{self.camera_name} is not connected on the LAN")
        # A 3.3 session brings no alerts (lan_alerts_local): the poll is their source.
        fast = poll_should_stay_fast(self.lan_connected, self.alerts_need_the_cloud or not self.lan_alerts_local)
        interval = POLL_FAST if fast else POLL_SLOW

        # A 3.4+ session pushes state and alarms as they happen, so polling the
        # cloud would fetch a copy (fork a59b77f, 619981b). The tick keeps its
        # interval, so the poll is back within 30 s of the session dropping,
        # and it still refreshes what only the cloud knows. The first refresh
        # always asks the cloud: it fills device_info and the local key.
        if self.cloud_poll_pausable:
            self._note_cloud_poll(paused=True)
            self.update_interval = interval
            try:
                if await self._fetch_rssi():
                    self._cloud_answered()
            except ConfigEntryAuthFailed as err:
                return self._session_rejected(err, interval)
            except CLOUD_FAILURES as err:
                # The session carries on; the outage is logged once each way.
                self._note_cloud_down(FAILURE_UNREACHABLE, err)
            if DPS_SENSEIQ_STATUS in self.data:
                await self._refresh_sleep_day()
            return self.data
        self._note_cloud_poll(paused=False)

        try:
            data = await self._fetch_from_cloud()
        except ConfigEntryAuthFailed as err:
            return self._session_rejected(err, interval)
        except TuyaAPIError as err:
            if is_auth_error(err):
                return self._session_rejected(err, interval)
            return self._without_cloud(FAILURE_API, err, interval)
        except UNREACHABLE_ERRORS as err:
            return self._without_cloud(FAILURE_UNREACHABLE, err, interval)

        # The LAN may have come back while this fetch ran.
        self.update_interval = None if self._cloud_poll_off else interval
        self._cloud_answered()
        # The cloud still answers for a camera that is off its Wi-Fi, with
        # the last values it had: shown as current, they looked live. Not on
        # the first fetch: failing that fails the setup of the whole entry,
        # every other camera included; the next poll marks it unavailable.
        if cloud_reports_offline(self.device_info) and not self.lan_connected:
            if not self._offline_logged:
                self._offline_logged = True
                _LOGGER.info("%s is offline according to the Tuya cloud and not on the LAN", self.camera_name)
            if self.data is not None:
                raise UpdateFailed(f"{self.camera_name} is offline")
            self._offline_at_start = True
        else:
            self._offline_logged = False
        await self._async_update_cache(data)
        return data

    @callback
    def _cloud_answered(self) -> None:
        """A cloud contact worked: close a failure episode with one line.

        Skipped when the last refresh raised: Home Assistant then logs its own
        recovery as soon as this one succeeds.
        """
        self._reauth_requested = False
        if self._cloud.succeeded():
            self._lan_only_logged = False
            if self.last_update_success:
                _LOGGER.info("Cloud for %s answers again", self.camera_name)

    def _session_rejected(self, err: Exception, interval: timedelta) -> dict:
        """The cloud refused the stored session, which only a reauth fixes.

        With a live LAN session the entities keep its state and the reauth flow
        starts once, as Home Assistant would (upstream bba4ab8): behind a paused
        poll a side call such as the WiFi signal may be the only cloud contact
        left. Without one the poll fails as it always has.
        """
        if not keep_serving_without_cloud(self.lan_live, self.data is not None):
            raise err if isinstance(err, ConfigEntryAuthFailed) else ConfigEntryAuthFailed(
                f"Authentication failed: {err}"
            ) from err
        self._cloud.failed(FAILURE_AUTH)
        self.update_interval = None if self._cloud_poll_off else interval
        if not self._reauth_requested:
            self._reauth_requested = True
            _LOGGER.warning(
                "Tuya session for %s is no longer valid (%s); asking to log in again "
                "(the LAN keeps running meanwhile)", self.camera_name, err,
            )
            if entry := getattr(self, "config_entry", None):
                entry.async_start_reauth(self.hass)
        return self.data

    def _without_cloud(self, kind: str, err: Exception, interval: timedelta) -> dict:
        """A failed cloud fetch: keep serving from the LAN, or fail the poll.

        Only a LAN session that proved its key keeps the entities available
        (lan_live). The retry backs off from the normal interval
        (cloud_retry_seconds).
        """
        self._cloud.failed(kind)
        self.update_interval = None if self._cloud_poll_off else timedelta(
            seconds=cloud_retry_seconds(self._cloud.failures, interval.total_seconds())
        )
        if keep_serving_without_cloud(self.lan_live, self.data is not None):
            self._note_cloud_down(kind, err, counted=True)
            return self.data
        raise UpdateFailed(f"Error fetching data: {err}") from err

    @callback
    def _note_cloud_down(self, kind: str, err: Exception, *, counted: bool = False) -> None:
        """A cloud failure the LAN session covers: one warning per outage, then debug."""
        if not counted:
            self._cloud.failed(kind)
        if self._lan_only_logged:
            _LOGGER.debug("Cloud for %s still failing: %s", self.camera_name, err)
            return
        self._lan_only_logged = True
        _LOGGER.warning(
            "Cloud for %s failed (%s); its LAN session keeps it available until the cloud answers again",
            self.camera_name, err,
        )

    async def _fetch_from_cloud(self) -> dict:
        """One cloud poll: device record, RSSI and the SenseIQ summary."""
        device = await self.api.get_device(self.camera_id)
        if not isinstance(device, dict):
            raise TuyaAPIError("NO_RESULT", "the cloud returned no device record")
        self.device_info = device
        self._adopt_local_key(device.get("localKey"))
        await self._refresh_rssi()
        api_dps = device.get("dps", {})
        # Only a SenseIQ monitor has a sleep summary; asking every other
        # model would cost a cloud call per poll for nothing.
        if DPS_SENSEIQ_STATUS in api_dps or DPS_SENSEIQ_STATUS in (self.data or {}):
            await self._refresh_sleep_day()
        changed = dps_delta(self.data, api_dps)
        if changed:
            _LOGGER.debug("Cloud poll changed DPS for %s: %s", self.camera_name, changed)
        if self.data:
            return {**self.data, **api_dps}
        return api_dps
