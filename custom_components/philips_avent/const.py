"""Constants for the Philips Avent Baby Monitor integration."""
from __future__ import annotations

from urllib.parse import quote

DOMAIN = "philips_avent"

# Tuya Mobile SDK credentials (static per APK version)
# These are functional identifiers, equivalent to OAuth client_id/secret
# in other HA integrations. Same for all users of the Philips Avent app.
TUYA_SIGNING_KEY = (
    "com.philips.ph.babymonitorplus"
    "_D2:D6:95:A1:1D:1B:84:F9:25:A9:45:6E:27:F4:45:E9:FD:87:C3:74"
    ":63:AA:8A:34:32:A6:6A:23:3B:0F:D5:0F"
    "_8n459nxk9g98gqgcwrpk3csv97uuwajm"
    "_a3nfht4ufwfw9cmkspaftv4x89cx58qx"
)
TUYA_APP_KEY = "wx3at9qprkhskvkcsyhm"
TUYA_PACKAGE_NAME = "com.philips.ph.babymonitorplus"
TUYA_CH_KEY = "071d81fa"

# Default data center (Central Europe). A Tuya account is bound to one data
# center and its session id is rejected by the others, so the real host is
# resolved per account at login time by `region.py` and persisted in the config
# entry. These two constants are only the fallback for entries created before
# that resolution existed (issues #44, #58).
TUYA_API_URL = "https://a1.tuyaeu.com/api.json"
TUYA_DEFAULT_COUNTRY_CODE = "39"

# MQTT is never addressed by a constant: the login response and
# `smartlife.m.user.info.get` both return the account's own broker in their
# `domain` block (`mobileMqttsUrl`, e.g. m1.tuyaeu.com for EU accounts,
# m1.tuyaus.com for American ones) and the bridge connects to that.
TUYA_MQTT_PORT = 8883

# Device model naming (issue #42). The Tuya productId does not distinguish
# models within the SCD9xx family — the same id ("selj2idknqhjnids") has been
# reported by both SCD951 and SCD953/26 units — so the displayed model stays
# generic. Add an entry here only when a productId is confirmed to belong to
# exactly one model.
PRODUCT_ID_TO_MODEL: dict[str, str] = {}
DEFAULT_MODEL = "Avent Baby Monitor"

# DPS codes
DPS_NIGHT_LIGHT = "138"
DPS_BRIGHTNESS = "158"
DPS_LIGHT_COLOR = "204"
DPS_LIGHT_TIMER = "240"
DPS_LIGHT_TIMER_SWITCH = "241"
DPS_TEMPERATURE = "207"
DPS_TEMPERATURE_F = "208"
DPS_MOTION_SWITCH = "134"
DPS_MOTION_SENSITIVITY = "106"
DPS_SOUND_SWITCH = "139"
DPS_SOUND_SENSITIVITY = "140"
DPS_LULLABY_CONTROL = "201"
DPS_LULLABY_VOLUME = "209"
DPS_LULLABY_MODE = "203"
DPS_LULLABY_STATE = "246"
DPS_LULLABY_TIMER_SWITCH = "243"
DPS_LULLABY_TIMER = "244"
DPS_PRIVACY_MODE = "237"
DPS_POWER_STATUS = "205"
DPS_FLIP = "102"
DPS_APP_TALKING = "253"
# Alert delivery differs by firmware and by negotiated LAN protocol version. A
# decrypted capture (#51) found none of these three in the LAN DP_QUERY set, and
# on a 3.3 session none of them ever arrived as a push. From 3.4 up DPS 212 does
# arrive: an SCD951 owner measured a motion record pushed with the sensor firing
# 1.3 seconds later, where the poll took 35 (#61), and an SCD973/26 pushed motion
# and sound records within 2.4 seconds (fork 619981b). coordinator.py therefore
# pauses the cloud poll behind a 3.4+ session once the monitor has reported in
# 212, and otherwise keeps the fast poll for monitors reporting alarms in 212.
DPS_ALERT_EVENT = "250"
DPS_DECIBEL_EVENT = "141"
# Alarm record with the snapshot the camera uploaded. The SCD951 and SCD953 on
# issues #61 and #42 report alerts here instead of on DPS 250, and so did an
# SCD973/26 in the fork, with 250 and 141 empty throughout: the key follows the
# firmware, not the model, so both are read on every monitor. See events.py for
# the payload. One slot holding the newest alarm, not a queue.
DPS_ALARM_RECORD = "212"
DPS_SENSEIQ_STATUS = "3"
DPS_SLEEP_SESSION = "4"
# More SenseIQ data points, from the product schema (productId 7d9t0rygsm7ztnww)
# read out of m.life.my.group.device.list. Confirmed live on an SCD9xx.
DPS_SENSEIQ_SWITCH = "1"       # sleepiq_switch  (bool) SenseIQ on/off
DPS_AWAKE_DELAY = "8"          # awake_delay     (value, seconds) awake-alert delay
DPS_AWAKE_SWITCH = "11"        # awake_switch    (bool) baby-awake alert on/off
DPS_CRY_DET_SWITCH = "12"      # cry_det_switch  (bool) cry alert on/off
DPS_NO_SENSEIQ_SIGNAL = "15"   # no_senseiq_signal (bool) True when SenseIQ has no
                               # signal (no baby detected / "scanning crib")
DPS_DEVICE_ERRORS = "18"       # device_errors   (value) 0 = no error

# The 15 tracks, in id order, which is also the order Next steps through them.
LULLABY_CATALOGUE = (
    ("Baa Baa Black Sheep", "lullabies"),
    ("Brahms' Lullaby", "lullabies"),
    ("Rock-a-Bye Baby", "lullabies"),
    ("Golden Slumbers", "lullabies"),
    ("Hush Little Baby", "lullabies"),
    ("Mother's Shh", "noise"),
    ("Calming River", "noise"),
    ("Heartbeat", "noise"),
    ("Vacuum Cleaner", "noise"),
    ("White Noise", "noise"),
    ("Garden Bird Song", "nature_sounds"),
    ("Valley Wind", "nature_sounds"),
    ("Ocean Shore", "nature_sounds"),
    ("Night-time Nature", "nature_sounds"),
    ("Rain Shower", "nature_sounds"),
)
# First id of each block the catalogue is published under. Units of the same
# product (kzm54lhabeeucq5a) report different blocks: the original SCD973 uses
# 3542154-3542168, a unit on firmware 1.1.12 uses 12072916-12072930 (issue #91),
# an SCD923 on firmware 1.4.0 uses 3219038-3219052 (issue #93).
# A camera ignores DPS 202 with an id outside its own block and keeps playing.
LULLABY_BLOCK_STARTS = (3542154, 12072916, 3219038)
LULLABY_TRACK_MAP = {
    start + index: track
    for start in LULLABY_BLOCK_STARTS
    for index, track in enumerate(LULLABY_CATALOGUE)
}
# Next and Prev move relative to the current track, so delivering one twice
# moves two tracks. The cloud forwards a published DPS to the camera, which is
# why a step already sent over the LAN must not be mirrored there (issue #91).
LULLABY_STEP_COMMANDS = frozenset({"next", "prev"})

TIMER_OPTIONS = {
    "Off": 0,
    "5 min": 300,
    "10 min": 600,
    "20 min": 1200,
    "30 min": 1800,
    "60 min": 3600,
    "90 min": 5400,
}
TIMER_SECONDS_TO_LABEL = {v: k for k, v in TIMER_OPTIONS.items()}

LULLABY_TRACKS = [name for name, _ in LULLABY_CATALOGUE]

CONF_SID = "sid"
CONF_API_HOST = "api_host"
# Phone device id, generated once and kept, see api.new_device_id and issue #73.
CONF_DEVICE_ID = "device_id"
CONF_COUNTRY_CODE = "country_code"
CONF_ECODE = "ecode"
CONF_PARTNER = "partner_identity"
CONF_UID = "uid"
CONF_CAMERA_ID = "camera_id"
CONF_CAMERA_NAME = "camera_name"
CONF_BRIDGE_PORT = "bridge_port"
DEFAULT_BRIDGE_PORT = 38554
CONF_BRIDGE_HOST = "bridge_host"
DEFAULT_BRIDGE_HOST = "localhost"
# The cameras' LAN addresses, comma-separated. The LAN client finds a camera by
# a broadcast scan, which never reaches Home Assistant when it runs in a
# container on a bridge network (no host networking). With addresses set, each
# camera's client tries them first and keeps the one whose handshake proves its
# key (protocol 3.4 and up), so one list serves every camera of the account;
# it scans only when none does. Empty: scan only, as before.
CONF_LAN_HOSTS = "lan_hosts"
DEFAULT_LAN_HOSTS = ""
# Two-way audio. Off by default: asking the camera for it makes the monitor stop
# a playing lullaby and restart it with a fresh timer when the stream closes
# (issue #72), so listening must not imply talking.
CONF_TALKBACK = "talkback"
DEFAULT_TALKBACK = False
CONF_LAN_STREAMING = "lan_streaming"
DEFAULT_LAN_STREAMING = True
# WebRTC over the LAN only. The bridge then uses no STUN/TURN servers and only
# the cameras' host candidates on a private address, so WebRTC media never
# leaves the local network (signalling still goes through Tuya's cloud). Off by
# default: it makes WebRTC fail when the camera is not on the bridge's LAN.
CONF_WEBRTC_LAN_ONLY = "webrtc_lan_only"
DEFAULT_WEBRTC_LAN_ONLY = False
# WebRTC at all. Off leaves only the local stream: the stream sources that use
# WebRTC are not offered, two-way audio is not asked and not sent, and the
# bridge refuses its WebRTC path and makes no MQTT connection. The options keep
# their stored values, so turning WebRTC back on brings them back as they were.
# On by default: for a camera without a local stream WebRTC is the only video.
CONF_WEBRTC = "webrtc"
DEFAULT_WEBRTC = True
# The bridge can end a stream this many minutes after it started, even with
# a client still connected (bridge field max_stream_minutes; 0 = no limit),
# so a screen left on a live view does not keep the camera streaming next to
# the parent unit (02-11-2026). No limit by default: a baby monitor is
# watched for longer than any fixed limit, and a stream that ends on its own
# leaves the card waiting for a new start (03-11-2026).
CONF_MAX_STREAM_MINUTES = "max_stream_minutes"
DEFAULT_MAX_STREAM_MINUTES = 0
# The bridge adds an AAC copy of each camera's G.711 audio (bridge field
# aac_audio), because Home Assistant's HLS keeps only AAC and MP3 and played
# the cameras without sound. Costs one ffmpeg per stream while a client takes
# that track; off leaves the streams as the camera sends them.
CONF_AAC_AUDIO = "aac_audio"
DEFAULT_AAC_AUDIO = True
# "Share the LAN connection while streaming" (bridge_share.py): off keeps the
# integration's own LAN session next to the bridge's while it streams; on hands
# the control traffic to the bridge's session, so the camera sees one. The
# token guards the bridge's endpoints for it; generated once, kept in the
# entry's data and written into the bridge config.
CONF_LAN_SHARE = "lan_share"
# On by default since 02-11-2026: measured on an SCD951, commands during a
# stream were confirmed in 0.007-0.25 s through the bridge against 0.7-5 s over
# a second session, with the same video, and the camera sees one connection.
# An entry that stored an explicit choice keeps it.
DEFAULT_LAN_SHARE = True
CONF_BRIDGE_API_TOKEN = "bridge_api_token"
# The password of the bridge's RTSP streams (Digest, user RTSP_USER), made
# once like the API token and written into the bridge config as
# rtsp_password: anything that reached the port could watch without it.
CONF_BRIDGE_RTSP_PASSWORD = "bridge_rtsp_password"
RTSP_USER = "homeassistant"
# The local stream's quality, per camera with a local stream: the "Local
# stream quality" select (lan_quality.py). It becomes the bridge's per-camera
# lan_clarity: the clarity word in the local stream's startup sequence
# (tuya-lan's media.go). The Avent app asks for standard (2), which is
# 1280x720; HD (4) is 1920x1080, measured on an SCD951 (01-11-2026).
LAN_QUALITY_HD = "hd"
LAN_QUALITY_SD = "sd"
LAN_QUALITIES = (LAN_QUALITY_HD, LAN_QUALITY_SD)
# HD (1080p), the camera's full picture. Standard (720p), what the Avent
# app asks for, costs the camera less next to the parent unit.
DEFAULT_LAN_QUALITY = LAN_QUALITY_HD
# LAN preferred: start from the offline cache and leave the cloud alone unless
# the cache cannot do the job (see PhilipsAventCoordinator.async_start). Off by
# default, because alerts some models report only through the cloud poll, app
# settings changes, the WiFi signal, the SenseIQ summary and video need it.
CONF_LAN_PREFERRED = "lan_preferred"
DEFAULT_LAN_PREFERRED = False


def lullaby_track_id(name: str, current_id: int | None) -> int | None:
    """The id to send in DPS 202 to play `name` on this camera.

    The block is taken from the id the camera last reported in DPS 248, since
    that is the only sign of which catalogue it holds. With nothing reported
    yet, the original block is assumed, which is what every release before
    issue #91 sent.
    """
    if name not in LULLABY_TRACKS:
        return None
    start = LULLABY_BLOCK_STARTS[0]
    if isinstance(current_id, int):
        for block in LULLABY_BLOCK_STARTS:
            if block <= current_id < block + len(LULLABY_CATALOGUE):
                start = block
                break
    return start + LULLABY_TRACKS.index(name)


def mirror_dps_to_cloud(dps: dict) -> bool:
    """Whether a command the LAN already delivered should also go to the cloud.

    The mirror keeps the cloud state in step, which is harmless for values and
    for play, pause and stop. A relative step is not idempotent, and the cloud
    forwards it to the camera, so Next would advance two tracks (issue #91).
    """
    return dps.get(DPS_LULLABY_CONTROL) not in LULLABY_STEP_COMMANDS


def sanitize_rtsp_path(name: str, cam_id: str) -> str:
    """Convert a camera display name into an RTSP path component.

    Spaces, forward slashes and backslashes are replaced with underscores.
    If the result is empty or consists only of an underscore, falls back to
    the camera id. Returns the path component WITHOUT a leading slash; the
    caller composes the URL.

    Mirrors `pkg/storage/path.go::SanitizeRTSPPath` in the Go bridge. The two
    helpers MUST stay in sync — they determine whether the integration's RTSP
    URL matches the path the bridge serves.
    """
    safe = name.replace(" ", "_").replace("/", "_").replace("\\", "_")
    if safe == "" or safe == "_":
        safe = cam_id
    return safe


# Where the bridge takes a camera's stream from, chosen per camera with the
# "Stream source" select. Each maps to a path suffix the bridge serves
# (pkg/rtsp/stream_mode.go); auto keeps the camera's plain path.
STREAM_SOURCE_AUTO = "auto"  # the LAN first, the cloud as fallback
STREAM_SOURCE_WEBRTC_FIRST = "webrtc_first"  # WebRTC first, the local stream as fallback
STREAM_SOURCE_LOCAL = "local"  # the LAN only
STREAM_SOURCE_WEBRTC = "webrtc"  # WebRTC only, set up through Tuya's cloud
STREAM_SOURCE_PATHS = {
    STREAM_SOURCE_AUTO: "",
    STREAM_SOURCE_WEBRTC_FIRST: "webrtc-first",
    STREAM_SOURCE_LOCAL: "local",
    STREAM_SOURCE_WEBRTC: "webrtc",
}


def build_rtsp_url(
    host: str, port: int, name: str, cam_id: str, *, stream_source: str | None = None,
    password: str | None = None,
) -> str:
    """Build the RTSP URL Home Assistant should pull the stream from.

    The host defaults to localhost, which only holds when the bridge runs
    alongside Home Assistant (add-on, or same container). A bridge in its own
    container or on another machine is not reachable there, and the resulting
    "Connection refused" makes the camera entity flap between Unavailable and
    Idle, because Home Assistant marks a camera unavailable while its stream
    cannot be opened (issue #62).

    ``stream_source`` appends that mode's suffix after the camera's path, so
    whatever decides the path (a collision suffix included) stays in one place.

    ``password`` puts the bridge's RTSP credentials in the URL. Such a URL is a
    secret: log ``redact_rtsp_url`` of it instead.
    """
    auth = f"{RTSP_USER}:{quote(password, safe='')}@" if password else ""
    url = f"rtsp://{auth}{host or DEFAULT_BRIDGE_HOST}:{port}/{sanitize_rtsp_path(name, cam_id)}"
    suffix = STREAM_SOURCE_PATHS.get(stream_source or STREAM_SOURCE_AUTO, "")
    return f"{url}/{suffix}" if suffix else url


def redact_rtsp_url(url: str) -> str:
    """``url`` without the credentials ``build_rtsp_url`` may have put in it."""
    scheme, sep, rest = url.partition("://")
    if not sep:
        return url
    authority, slash, path = rest.partition("/")
    if "@" in authority:
        authority = "***@" + authority.rsplit("@", 1)[1]
    return f"{scheme}://{authority}{slash}{path}"
