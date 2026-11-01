from const import DEFAULT_BRIDGE_HOST, build_rtsp_url, rtsp_paths, sanitize_rtsp_path


def test_simple_name():
    assert sanitize_rtsp_path("Erik", "abc123") == "Erik"


def test_name_with_spaces():
    assert sanitize_rtsp_path("Baby Room", "abc123") == "Baby_Room"


def test_name_with_slash():
    assert sanitize_rtsp_path("Baby/Room", "abc123") == "Baby_Room"


def test_name_with_backslash():
    assert sanitize_rtsp_path("Baby\\Room", "abc123") == "Baby_Room"


def test_empty_name_falls_back_to_id():
    assert sanitize_rtsp_path("", "abc123") == "abc123"


def test_underscore_only_falls_back_to_id():
    # Name made entirely of separators sanitizes to "_" → use id instead
    assert sanitize_rtsp_path("/", "abc123") == "abc123"


def test_multiple_spaces():
    assert sanitize_rtsp_path("Erik  Two", "abc123") == "Erik__Two"


class TestBuildRtspUrl:
    """The URL Home Assistant pulls the stream from (issue #62).

    A bridge running in its own container is not on localhost, and an
    unreachable stream makes Home Assistant mark the camera unavailable, which
    is what produced the Unavailable/Idle flapping.
    """

    def test_default_host_matches_previous_behaviour(self):
        assert build_rtsp_url(DEFAULT_BRIDGE_HOST, 38554, "Erik", "abc123") == "rtsp://localhost:38554/Erik"

    def test_custom_host_and_port(self):
        url = build_rtsp_url("aventproxy-bridge", 8554, "Baby Room", "abc123")
        assert url == "rtsp://aventproxy-bridge:8554/Baby_Room"

    def test_ip_address_host(self):
        assert build_rtsp_url("192.168.1.50", 38554, "Erik", "abc") == "rtsp://192.168.1.50:38554/Erik"

    def test_empty_host_falls_back_to_the_default(self):
        assert build_rtsp_url("", 38554, "Erik", "abc") == "rtsp://localhost:38554/Erik"

    def test_path_uses_the_shared_sanitiser(self):
        # Must stay identical to the path the Go bridge serves.
        name, cam_id = "Baby/Room 2", "dev1"
        url = build_rtsp_url("host", 38554, name, cam_id)
        assert url.endswith("/" + sanitize_rtsp_path(name, cam_id))

    def test_unnamed_camera_falls_back_to_the_id(self):
        assert build_rtsp_url("host", 38554, "", "dev1") == "rtsp://host:38554/dev1"


# rtsp_paths mirrors assignPaths in avent-webrtc-bridge/cmd/addon/addon.go; the
# cases below are the TestAssignPaths_* cases in addon_test.go.

def test_paths_without_collision():
    assert rtsp_paths([("abc123", "Erik"), ("def456", "Anna")]) == {"abc123": "Erik", "def456": "Anna"}


def test_paths_colliding_names_get_the_id_prefix():
    assert rtsp_paths([("abc123", "Baby"), ("def456", "Baby")]) == {"abc123": "Baby", "def456": "Baby_def456"}


def test_paths_short_id_used_in_full():
    assert rtsp_paths([("ab", "X"), ("cd", "X")])["cd"] == "X_cd"


def test_paths_cascading_fallback():
    paths = rtsp_paths([("abcdef01", "Baby"), ("abcdef02", "Baby"), ("abcdef03", "Baby")])
    assert list(paths.values()) == ["Baby", "Baby_abcdef", "Baby_abcdef_2"]


def test_paths_repeated_id_skipped():
    assert rtsp_paths([("abc123", "Baby"), ("abc123", "Baby")]) == {"abc123": "Baby"}


def test_url_uses_the_assigned_path():
    url = build_rtsp_url("192.0.2.10", 38554, "Baby", "def456", "Baby_def456")
    assert url == "rtsp://192.0.2.10:38554/Baby_def456"
