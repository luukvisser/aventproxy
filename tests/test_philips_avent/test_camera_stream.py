"""The camera entity reads the bridge over TCP and never starts a stream for a snapshot.

camera.py imports Home Assistant, which the unit tests do not install, so
these checks read its source, as other tests here do for entity code.
"""
from pathlib import Path

CAMERA = (Path(__file__).parents[2] / "custom_components" / "philips_avent" / "camera.py").read_text()


def test_stream_over_tcp_with_wallclock_timestamps():
    assert 'self.stream_options[CONF_RTSP_TRANSPORT] = "tcp"' in CAMERA
    assert "self.stream_options[CONF_USE_WALLCLOCK_AS_TIMESTAMPS] = True" in CAMERA


def test_snapshot_from_the_running_stream_first():
    body = CAMERA.split("async def async_camera_image", 1)[1]
    assert body.index("stream.async_get_image") < body.index("ffmpeg_get_image")


def test_snapshot_asks_the_stream_only_while_it_runs():
    # Stream.async_get_image starts a stream that is not running; a stream
    # that ran once stays `available`, so that alone is not enough.
    body = CAMERA.split("async def async_camera_image", 1)[1]
    assert body.index("if stream_running(stream):") < body.index("stream.async_get_image")
    helper = CAMERA.split("def stream_running", 1)[1].split("\nclass ", 1)[0]
    assert "any(not output.idle for output in stream.outputs().values())" in helper
    # Before its first keyframe the running stream has no picture: no second
    # connection through ffmpeg then.
    assert "return image or self._last_image" in body.split("ffmpeg_get_image", 1)[0]


def test_snapshot_through_the_bridge_at_most_every_ten_minutes():
    assert "SNAPSHOT_MIN_INTERVAL = 600" in CAMERA
    assert 'f"-rtsp_transport tcp -i {self._stream_url}"' in CAMERA
    assert "now - self._last_grab < SNAPSHOT_MIN_INTERVAL" in CAMERA
