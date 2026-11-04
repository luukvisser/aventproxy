package rtsp

import (
	"context"
	"errors"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/storage"
	"avent-webrtc-bridge/pkg/tuya"

	tuyalan "github.com/luukvisser/tuya-lan"
	"github.com/pion/rtp"
)

func TestParseStreamPath(t *testing.T) {
	for _, tc := range []struct {
		url, path, res string
		mode           StreamMode
	}{
		{"rtsp://localhost:38554/Cam", "/Cam", "hd", ModeAuto},
		{"rtsp://localhost:38554/Cam/", "/Cam", "hd", ModeAuto},
		{"rtsp://localhost:38554/Cam/local", "/Cam", "hd", ModeLocal},
		{"rtsp://localhost:38554/Cam/webrtc", "/Cam", "hd", ModeWebRTC},
		{"rtsp://localhost:38554/Cam/webrtc-first", "/Cam", "hd", ModeWebRTCFirst},
		{"rtsp://localhost:38554/Cam/sd", "/Cam", "sd", ModeAuto},
		{"rtsp://localhost:38554/Cam/hd", "/Cam", "hd", ModeAuto},
		{"rtsp://localhost:38554/Cam/sd/webrtc", "/Cam", "sd", ModeWebRTC},
		{"rtsp://localhost:38554/Cam/webrtc/sd", "/Cam", "sd", ModeWebRTC},
		// Track suffixes come after the mode, as the SDP's a=control builds them.
		{"rtsp://localhost:38554/Cam/local/video", "/Cam", "hd", ModeLocal},
		{"rtsp://localhost:38554/Cam/webrtc/audio", "/Cam", "hd", ModeWebRTC},
		{"rtsp://localhost:38554/Cam/hd/local/backchannel", "/Cam", "hd", ModeLocal},
		{"rtsp://localhost:38554/Cam/video", "/Cam", "hd", ModeAuto},
		// The camera is always the first segment, whatever its name.
		{"rtsp://localhost:38554/local", "/local", "hd", ModeAuto},
		{"rtsp://localhost:38554/local/local", "/local", "hd", ModeLocal},
		{"rtsp://localhost:38554/video/webrtc/video", "/video", "hd", ModeWebRTC},
		// A collision suffix is part of the camera segment.
		{"rtsp://localhost:38554/Cam_f6e5d4/local", "/Cam_f6e5d4", "hd", ModeLocal},
		// Unknown or repeated segments keep the whole path, so the lookup 404s.
		{"rtsp://localhost:38554/Cam/foo", "/Cam/foo", "hd", ModeAuto},
		{"rtsp://localhost:38554/Cam/local/webrtc", "/Cam/local/webrtc", "hd", ModeAuto},
		{"rtsp://localhost:38554/Cam/video/local", "/Cam/video/local", "hd", ModeAuto},
		{"rtsp://localhost:38554/", "", "", ModeAuto},
		{"rtsp://localhost:38554", "", "", ModeAuto},
	} {
		got := parseStreamPath(tc.url)
		if got.path != tc.path || got.resolution != tc.res || got.mode != tc.mode {
			t.Errorf("%s: got %+v, want path=%q res=%q mode=%q", tc.url, got, tc.path, tc.res, tc.mode)
		}
	}
}

func TestStreamKeyKeepsTheAutoKey(t *testing.T) {
	if got := streamKey("dev", "hd", ModeAuto); got != "dev-hd" {
		t.Fatalf("auto key = %q, want the key the bridge always used", got)
	}
	keys := map[string]bool{}
	for _, m := range []StreamMode{ModeAuto, ModeLocal, ModeWebRTC, ModeWebRTCFirst} {
		keys[streamKey("dev", "hd", m)] = true
	}
	if len(keys) != 4 {
		t.Fatalf("keys %v are not distinct per mode", keys)
	}
}

func TestModeEndpoints(t *testing.T) {
	with := strings.Join(modeEndpoints("rtsp://localhost:1/Cam", true, true), " ")
	if !strings.Contains(with, "/Cam/local") || !strings.Contains(with, "/Cam/webrtc ") || !strings.Contains(with, "/Cam/webrtc-first") {
		t.Fatalf("endpoints with LAN = %q", with)
	}
	without := strings.Join(modeEndpoints("rtsp://localhost:1/Cam", false, true), " ")
	if strings.Contains(without, "/Cam/local") || strings.Contains(without, "webrtc-first") || !strings.Contains(without, "/Cam/webrtc") {
		t.Fatalf("endpoints without LAN = %q", without)
	}
	noWebRTC := strings.Join(modeEndpoints("rtsp://localhost:1/Cam", true, false), " ")
	if !strings.Contains(noWebRTC, "/Cam/local") || strings.Contains(noWebRTC, "webrtc") {
		t.Fatalf("endpoints without WebRTC = %q", noWebRTC)
	}
}

// newModeStream registers a stream of one mode the way getOrCreateStreamMode
// does, with one client, and returns that client's end of the connection.
func newModeStream(t *testing.T, server *RTSPServer, mode StreamMode) (*CameraStream, net.Conn) {
	t.Helper()
	camera := &storage.CameraInfo{DeviceID: "testdev", DeviceName: "Test Cam"}
	cs, err := server.getOrCreateStreamMode(camera, "hd", mode, &storage.UserSession{UserKey: "user1"})
	if err != nil {
		t.Fatalf("%s stream: %v", mode, err)
	}
	cs.clientArrived()
	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() {
		clientConn.Close()
		serverConn.Close()
	})
	cs.clients["s1"] = &RTSPClient{conn: serverConn, session: "s1", stream: cs}
	return cs, clientConn
}

func assertConnClosed(t *testing.T, conn net.Conn) {
	t.Helper()
	_ = conn.SetReadDeadline(time.Now().Add(500 * time.Millisecond))
	if _, err := conn.Read(make([]byte, 1)); !errors.Is(err, io.EOF) {
		t.Fatalf("client connection not closed: %v", err)
	}
}

func TestStreamsAreSeparatedByMode(t *testing.T) {
	server := newLANServer()
	auto, _ := newModeStream(t, server, ModeAuto)
	local, _ := newModeStream(t, server, ModeLocal)
	cloud, _ := newModeStream(t, server, ModeWebRTC)
	first, _ := newModeStream(t, server, ModeWebRTCFirst)
	ids := map[string]bool{auto.streamId: true, local.streamId: true, cloud.streamId: true, first.streamId: true}
	if len(ids) != 4 || len(server.streams) != 4 {
		t.Fatalf("stream ids %v, %d in the server map", ids, len(server.streams))
	}
	if auto.streamId != "testdev-hd" {
		t.Fatalf("auto stream id %q", auto.streamId)
	}
	// A running local stream is handed out again.
	local.mutex.Lock()
	local.claimStart()
	local.mutex.Unlock()
	again, err := server.getOrCreateStreamMode(local.camera, "hd", ModeLocal, local.user)
	if err != nil || again != local {
		t.Fatalf("a second local request got %p (%v), want the running local stream", again, err)
	}
}

func TestLocalModeRefusedWithoutLANPath(t *testing.T) {
	server := newLANServer()
	server.LANTargets["testdev"] = LANTarget{LocalKey: "lk", Protocol: "3.5", IP: "192.0.2.10"} // no password
	camera := &storage.CameraInfo{DeviceID: "testdev", DeviceName: "Test Cam"}
	if _, err := server.getOrCreateStreamMode(camera, "hd", ModeLocal, &storage.UserSession{}); !errors.Is(err, errLocalUnavailable) {
		t.Fatalf("err = %v, want errLocalUnavailable", err)
	}
	if len(server.streams) != 0 {
		t.Fatalf("a refused local request left %d streams", len(server.streams))
	}
	// With a LAN path, the SD stream still has none.
	server = newLANServer()
	if _, err := server.getOrCreateStreamMode(camera, "sd", ModeLocal, &storage.UserSession{}); !errors.Is(err, errLocalUnavailable) {
		t.Fatalf("sd: err = %v, want errLocalUnavailable", err)
	}
}

func TestLocalModeServesOverTheLAN(t *testing.T) {
	server := newLANServer()
	server.MobileClient = &tuya.MobileSDKClient{}
	server.Talkback = true // the local stream simply has no return audio
	cs, _ := newModeStream(t, server, ModeLocal)
	if cs.webrtcBridge.mobileClient != nil {
		t.Fatal("a local-only stream was given the cloud client")
	}
	a := fakeAttempts(t, cs, nil)
	begin(cs)

	if active, _, l := streamState(cs); !active || l != a.lan {
		t.Fatalf("active=%v lan=%v, want the LAN source serving", active, l)
	}
	if a.cloudStarted.Load() != 0 {
		t.Fatalf("cloud started %d times for a local-only stream", a.cloudStarted.Load())
	}
	if !server.lanSessions.busy("testdev") {
		t.Fatal("the local stream does not hold the camera's LAN session")
	}
	cs.stopStream()
	if server.lanSessions.busy("testdev") {
		t.Fatal("a stopped stream kept the camera's LAN session")
	}
}

// A local-only stream whose LAN path fails ends its clients and never uses
// the cloud; nor does the failure start the auto stream's cool-down.
func TestLocalModeNeverFallsBackToTheCloud(t *testing.T) {
	server := newLANServer()
	cs, clientConn := newModeStream(t, server, ModeLocal)
	a := fakeAttempts(t, cs, func(context.Context) error { return errors.New("lan: 3.4 handshake: connection refused") })

	begin(cs)

	if active, connecting, l := streamState(cs); active || connecting || l != nil {
		t.Fatalf("active=%v connecting=%v lan=%v after a failed local-only start", active, connecting, l)
	}
	if a.cloudStarted.Load() != 0 {
		t.Fatal("the local-only stream used the cloud")
	}
	if !a.lan.isStopped() {
		t.Fatal("the failed LAN source was left running")
	}
	assertConnClosed(t, clientConn)
	if server.lanCoolingDown("testdev") {
		t.Fatal("a local-only failure started the auto stream's cool-down")
	}
	if server.lanSessions.busy("testdev") {
		t.Fatal("the failed stream kept the camera's LAN session")
	}
}

func TestLocalModeDeathKeepsTheCooldown(t *testing.T) {
	server := newLANServer()
	cs, clientConn := newModeStream(t, server, ModeLocal)
	a := fakeAttempts(t, cs, nil)
	begin(cs)

	a.lan.die(errors.New("lan: Test Cam stopped streaming: EOF"))

	assertConnClosed(t, clientConn)
	if server.lanCoolingDown("testdev") || server.lanSessions.busy("testdev") {
		t.Fatalf("cooling down %v, LAN busy %v after a local stream died; want neither",
			server.lanCoolingDown("testdev"), server.lanSessions.busy("testdev"))
	}
}

func TestOneLANSessionPerCamera(t *testing.T) {
	server := newLANServer()
	auto, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, auto, nil)
	begin(auto)
	if !server.lanSessions.busy("testdev") {
		t.Fatal("the auto stream on the LAN does not hold the session")
	}
	camera := &storage.CameraInfo{DeviceID: "testdev", DeviceName: "Test Cam"}
	if _, err := server.getOrCreateStreamMode(camera, "hd", ModeLocal, &storage.UserSession{}); !errors.Is(err, errLANBusy) {
		t.Fatalf("local while the auto stream holds the LAN: err = %v, want errLANBusy", err)
	}
	auto.stopStream()

	// The other way round: a local stream holds it, and the auto stream
	// takes the cloud without counting that as a LAN failure.
	local, _ := newModeStream(t, server, ModeLocal)
	fakeAttempts(t, local, nil)
	begin(local)
	waitStreamRemoved(t, server, auto.streamId)
	next, _ := newModeStream(t, server, ModeAuto)
	b := fakeAttempts(t, next, nil)
	begin(next)
	if b.lanBuilt.Load() != 0 || b.cloudStarted.Load() != 1 {
		t.Fatalf("auto beside a local stream: lan built %d, cloud started %d; want 0 and 1", b.lanBuilt.Load(), b.cloudStarted.Load())
	}
	if server.lanCoolingDown("testdev") {
		t.Fatal("a busy LAN session started the cool-down")
	}
	next.stopStream()
	local.stopStream()
}

func TestWebRTCModeSkipsTheLAN(t *testing.T) {
	server := newLANServer()
	server.startLANCooldown("testdev") // would arm a promotion on an auto stream
	cs, _ := newModeStream(t, server, ModeWebRTC)
	a := fakeAttempts(t, cs, nil)
	begin(cs)
	if a.lanBuilt.Load() != 0 || a.cloudStarted.Load() != 1 {
		t.Fatalf("lan built %d, cloud started %d; want 0 and 1", a.lanBuilt.Load(), a.cloudStarted.Load())
	}
	if promoteTimer(cs) != nil {
		t.Fatal("a WebRTC-only stream was armed to move to the LAN")
	}
	cs.stopStream()
}

func TestWebRTCFirstStaysOnTheCloudWithVideo(t *testing.T) {
	server := newLANServer()
	cs, _ := newModeStream(t, server, ModeWebRTCFirst)
	a := fakeAttempts(t, cs, nil)
	cloud := cs.cloudStart
	cs.cloudStart = func() error {
		err := cloud()
		cs.webrtcBridge.forwardVideo(&rtp.Packet{Payload: []byte{0x65, 0x88}})
		return err
	}
	begin(cs)
	if active, _, _ := streamState(cs); !active || a.lanBuilt.Load() != 0 || a.cloudStarted.Load() != 1 {
		t.Fatalf("active %v, lan built %d, cloud started %d; want the cloud only", active, a.lanBuilt.Load(), a.cloudStarted.Load())
	}
	cs.stopStream()
}

func TestWebRTCFirstFallsBackToTheLAN(t *testing.T) {
	t.Run("cloud does not start", func(t *testing.T) {
		server := newLANServer()
		cs, _ := newModeStream(t, server, ModeWebRTCFirst)
		a := fakeAttempts(t, cs, nil)
		cs.cloudStart = func() error {
			a.cloudStarted.Add(1)
			return errors.New("no answer")
		}
		begin(cs)
		if active, _, l := streamState(cs); !active || l != a.lan || a.cloudStarted.Load() != 1 {
			t.Fatalf("active %v, lan %v, cloud started %d; want the LAN after one cloud attempt", active, l, a.cloudStarted.Load())
		}
		cs.stopStream()
	})
	t.Run("WebRTC error before video", func(t *testing.T) {
		server := newLANServer()
		cs, _ := newModeStream(t, server, ModeWebRTCFirst)
		a := fakeAttempts(t, cs, nil)
		cs.cloudStart = func() error {
			a.cloudStarted.Add(1)
			bridge := cs.webrtcBridge
			go cs.handleBridgeError(bridge, errors.New("ICE failed"))
			return nil
		}
		start := time.Now()
		begin(cs)
		if active, _, l := streamState(cs); !active || l != a.lan {
			t.Fatalf("active %v, lan %v; want the LAN", active, l)
		}
		if waited := time.Since(start); waited >= webrtcFirstVideoDeadline {
			t.Fatalf("the WebRTC error was only noticed at the deadline (%v)", waited)
		}
		cs.stopStream()
	})
}

// With WebRTC off, auto and webrtc-first are the local stream (one shared
// stream) and webrtc is refused.
func TestNoWebRTCServesOnlyTheLocalStream(t *testing.T) {
	server := newLANServer()
	server.NoWebRTC = true
	camera := &storage.CameraInfo{DeviceID: "testdev", DeviceName: "Test Cam"}
	user := &storage.UserSession{UserKey: "user1"}
	if _, err := server.getOrCreateStreamMode(camera, "hd", ModeWebRTC, user); !errors.Is(err, errWebRTCOff) {
		t.Fatalf("webrtc: err = %v, want errWebRTCOff", err)
	}
	auto, err := server.getOrCreateStreamMode(camera, "hd", ModeAuto, user)
	if err != nil || auto.mode != ModeLocal {
		t.Fatalf("auto: %v, mode %v; want the local stream", err, auto)
	}
	first, err := server.getOrCreateStreamMode(camera, "hd", ModeWebRTCFirst, user)
	if err != nil || first != auto {
		t.Fatalf("webrtc-first got %p (%v), want the local stream %p", first, err, auto)
	}

	// A camera without a LAN path has no stream at all.
	server.LANTargets = map[string]LANTarget{}
	other := &storage.CameraInfo{DeviceID: "other", DeviceName: "Other"}
	if _, err := server.getOrCreateStreamMode(other, "hd", ModeAuto, user); !errors.Is(err, errLocalUnavailable) {
		t.Fatalf("auto without a LAN path: err = %v, want errLocalUnavailable", err)
	}
}

// A stream ends at MaxStreamDuration even with a client still connected, so a
// screen left on a live view does not keep the camera streaming (02-11-2026).
func TestStreamEndsAtMaxDuration(t *testing.T) {
	server := newLANServer()
	server.MaxStreamDuration = 50 * time.Millisecond
	cs, clientConn := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, cs, nil)
	begin(cs)

	assertConnClosed(t, clientConn)
	waitStreamRemoved(t, server, cs.streamId)
	cs.mutex.RLock()
	active, timer := cs.active.Load(), cs.limitTimer
	cs.mutex.RUnlock()
	if active || timer != nil {
		t.Fatalf("stream still active=%v or timer left=%v after the limit", active, timer != nil)
	}
}

func TestNoLimitWithoutMaxDuration(t *testing.T) {
	cs, _ := newModeStream(t, newLANServer(), ModeAuto)
	fakeAttempts(t, cs, nil)
	begin(cs)
	cs.mutex.RLock()
	timer := cs.limitTimer
	cs.mutex.RUnlock()
	if timer != nil {
		t.Fatal("a limit timer without MaxStreamDuration")
	}
	cs.stopStream()
}

// The limit of a run that ended fires late, while the stream has started
// again: it must not end the new run.
func TestAnEarlierRunsLimitEndsNothing(t *testing.T) {
	server := newLANServer()
	server.MaxStreamDuration = time.Hour
	cs, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, cs, nil)
	begin(cs)
	cs.mutex.Lock()
	first := cs.limitGen
	cs.stopStreamInternal()
	if cs.limitTimer != nil {
		cs.mutex.Unlock()
		t.Fatal("limit timer left running after the stream stopped")
	}
	cs.mutex.Unlock()
	begin(cs)

	cs.endAtLimit(first)
	cs.mutex.RLock()
	active, timer := cs.active.Load(), cs.limitTimer
	cs.mutex.RUnlock()
	if !active || timer == nil {
		t.Fatalf("the earlier run's limit ended the new run (active=%v, timer=%v)", active, timer != nil)
	}
	cs.stopStream()
}

// A camera whose local stream is set to standard asks WebRTC for "sd" too.
func TestWebRTCResolutionFollowsTheLocalQuality(t *testing.T) {
	server := NewRTSPServer(0, nil)
	server.LANTargets = map[string]LANTarget{
		"std": {Clarity: tuyalan.ClarityStandard},
		"hd":  {Clarity: tuyalan.ClarityHD},
	}
	for _, tc := range []struct{ id, resolution, want string }{
		{"std", "hd", "sd"},
		{"std", "sd", "sd"},
		{"hd", "hd", "hd"},
		{"cloud", "hd", "hd"},
	} {
		if got := server.webrtcResolution(tc.id, tc.resolution); got != tc.want {
			t.Errorf("%s at %s: %s, want %s", tc.id, tc.resolution, got, tc.want)
		}
	}
	var none *RTSPServer
	if got := none.webrtcResolution("std", "hd"); got != "hd" {
		t.Errorf("nil server: %s", got)
	}
}
