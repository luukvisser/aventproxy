"""What to do when the Tuya cloud cannot be reached.

The camera's LAN session (Tuya protocol on TCP 6668, with its localKey) carries
status, commands and, on 3.4+, motion alerts on its own. Only the key and the
device details come from the cloud, so a cloud outage at startup used to keep
the whole integration in setup-retry. These decisions let it start on the LAN
from what an earlier cloud fetch left in the cache (offline_cache.py), and
return to the cloud by itself.

Kept free of Home Assistant imports so the decisions can be unit-tested without
an HA runtime, like lan_policy.py; coordinator.py owns the state.
"""
from __future__ import annotations

import time

import aiohttp

try:
    from .api import TuyaAPIError
except ImportError:  # loaded standalone by unit tests
    from api import TuyaAPIError

# aiohttp's default total timeout is five minutes, which kept setup waiting that
# long on a cloud that does not answer before anything could fall back.
CLOUD_TIMEOUT = 30

# Error codes that mean the session itself is gone; these must reach the user as
# a reauth prompt, never be papered over by the LAN.
AUTH_ERROR_CODES = ("SID_INVALID", "USER_SESSION_LOSS")

# What a cloud call raises when the cloud is not there: DNS and connection
# errors and timeouts (aiohttp.ClientError, TimeoutError), and a ValueError from
# decoding a reply that is not Tuya's JSON (a proxy or captive-portal page).
UNREACHABLE_ERRORS = (aiohttp.ClientError, TimeoutError, OSError, ValueError)

FAILURE_AUTH = "auth"
FAILURE_UNREACHABLE = "unreachable"
FAILURE_API = "api"

START_LAN_ONLY = "lan_only"
START_AUTH_FAILED = "auth_failed"
START_NOT_READY = "not_ready"

# Ceiling for the cloud retry while it is away. The retry starts at the normal
# poll interval and doubles from there.
CLOUD_RETRY_MAX = 600.0


# "LAN preferred" asks the cloud for the device record when the camera refuses
# the cached key (a re-pair gives it a new one), at most this often per camera,
# so a camera that refuses for another reason does not turn into a cloud poll.
KEY_REFRESH_INTERVAL = 3600.0

# ... and once in this interval for firmware and name changes, which the LAN
# never reports.
DEVICE_INFO_REFRESH_INTERVAL = 86400.0

# A camera in LAN preferred that has not been on the LAN for this long is
# polled through the cloud until its LAN session is back, instead of staying
# unavailable: a camera with a new DHCP lease is not found by a scan when Home
# Assistant runs in a container on a bridge network.
LAN_FALLBACK_DELAY = 300.0

# The cloud fallback ends once the LAN has stayed up this long. Ending it on
# every reconnect let a camera on poor Wi-Fi, back for a moment every few
# minutes, cancel each fallback before it began: no cloud poll at all.
LAN_STABLE_DELAY = 60.0

# In LAN preferred, a LAN that comes back after a drop gets one cloud fetch,
# at most this often: the firmware answers no DP_QUERY, so whatever changed
# while it was away (a lullaby that ended, an alarm, a setting) would show
# stale until its next push.
LAN_RESYNC_INTERVAL = 600.0


def cloud_reports_offline(device: dict | None) -> bool:
    """Whether Tuya's device record says the camera is not connected to it.

    `isOnline` is part of tuya.m.device.get's answer (upstream's
    examples/camera_control.py reads it). A record without it says nothing.
    """
    return isinstance(device, dict) and device.get("isOnline") is False


def refresh_allowed(last: float | None, now: float, interval: float = KEY_REFRESH_INTERVAL) -> bool:
    """Whether a rate-limited cloud refresh may run now."""
    return last is None or now - last >= interval


def is_auth_error(err: BaseException) -> bool:
    """True when a Tuya error means the stored session is no longer valid."""
    return isinstance(err, TuyaAPIError) and any(code in str(err) for code in AUTH_ERROR_CODES)


def classify_cloud_error(err: BaseException) -> str:
    """Sort a failed cloud call into auth, unreachable or another API error.

    A TuyaAPIError is an answer from the cloud, so only the auth codes and the
    rest are told apart there; anything in UNREACHABLE_ERRORS means no usable
    answer came back at all.
    """
    if isinstance(err, TuyaAPIError):
        return FAILURE_AUTH if is_auth_error(err) else FAILURE_API
    if isinstance(err, UNREACHABLE_ERRORS):
        return FAILURE_UNREACHABLE
    return FAILURE_API


def startup_mode(failure: str | None, cache_usable: bool) -> str:
    """How to proceed after the first cloud refresh failed.

    Only an unreachable cloud starts on the LAN, and only with a cached key: an
    auth error must prompt for a new login, and a Tuya error answer is not an
    outage the LAN should hide at startup. Everything else is the setup-retry
    the integration has always done.
    """
    if failure == FAILURE_AUTH:
        return START_AUTH_FAILED
    if failure == FAILURE_UNREACHABLE and cache_usable:
        return START_LAN_ONLY
    return START_NOT_READY


def cloud_retry_seconds(failures: int, base: float, cap: float = CLOUD_RETRY_MAX) -> float:
    """Seconds until the next cloud attempt after `failures` failures in a row.

    The first retry comes at the normal poll interval, each one after that waits
    twice as long, up to `cap`, so a long outage costs a request every ten
    minutes rather than every poll.
    """
    if failures <= 1:
        return min(base, cap)
    return min(base * 2 ** (failures - 1), cap)


class AccountCloud:
    """The last contact with the cloud for the whole account (config entry).

    Every camera of an entry talks to the cloud with the same session and
    host, so a contact made for one camera answers for all: in LAN preferred
    a camera started from the cache makes none of its own for up to a day,
    and its Cloud Connection showed unknown while another camera's fetch had
    just worked. Listeners (each coordinator) are told about every contact.
    """

    def __init__(self) -> None:
        self.available: bool | None = None
        self.checked_at: float | None = None
        self._listeners: list = []

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def note(self, ok: bool) -> None:
        self.available = ok
        self.checked_at = time.time()
        for listener in list(self._listeners):
            listener()


class CloudState:
    """Whether the cloud answered last time, and how often it failed in a row.

    `available` is None until the first attempt, and `checked_at` (epoch
    seconds) says when the last one was. The return values of `failed` and
    `succeeded` say whether the state just changed, so the caller logs a
    transition once rather than on every poll.
    """

    def __init__(self, account: AccountCloud | None = None) -> None:
        self.available: bool | None = None
        self.checked_at: float | None = None
        self.failures = 0
        self.last_failure: str | None = None
        # The account's view, told about every contact this camera makes.
        self.account = account

    def failed(self, kind: str) -> bool:
        """Record a failure; True when the cloud was not already known to be away."""
        went_away = self.available is not False
        self.available = False
        self.checked_at = time.time()
        if self.account:
            self.account.note(False)
        self.failures += 1
        self.last_failure = kind
        return went_away

    def succeeded(self) -> bool:
        """Record a success; True when this ends an outage."""
        came_back = self.available is False
        self.available = True
        self.checked_at = time.time()
        if self.account:
            self.account.note(True)
        self.failures = 0
        self.last_failure = None
        return came_back


def keep_serving_without_cloud(lan_connected: bool, has_data: bool) -> bool:
    """Whether entities stay available while the cloud poll fails.

    They do as long as the LAN session is up and there is state to show: the
    LAN keeps the DPS fresh. Without the session there is nothing current, and
    the poll failure marks them unavailable as it always has.
    """
    return lan_connected and has_data
