package rtsp

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/lan"
	"avent-webrtc-bridge/pkg/storage"
)

// A camera that serves one LAN session at a time (protocol 3.3): the bridge
// asks the integration to release its own session and connects only after
// the confirmation; at the end it closes first.

func TestShareRequestSessionWaitsForTheRelease(t *testing.T) {
	var h ShareHub
	got := make(chan bool, 1)
	go func() { got <- h.requestSession(context.Background(), "cam", 2*time.Second) }()
	waitFor(t, "wants_session", func() bool { return h.counters("cam").WantsSession })
	if !h.noteReleased("cam") {
		t.Fatal("the release found no waiting start")
	}
	if !<-got {
		t.Fatal("requestSession did not report the release")
	}
	if h.counters("cam").WantsSession {
		t.Fatal("wants_session stayed on after the release")
	}
	if h.noteReleased("cam") {
		t.Fatal("a release without a waiting start was accepted")
	}
}

func TestShareRequestSessionGivesUp(t *testing.T) {
	var h ShareHub
	started := time.Now()
	if h.requestSession(context.Background(), "cam", 50*time.Millisecond) {
		t.Fatal("reported a release that never came")
	}
	if time.Since(started) > time.Second {
		t.Fatal("waited past its limit")
	}
	if h.counters("cam").WantsSession {
		t.Fatal("wants_session stayed on after giving up")
	}
}

// A stream stopped while it waits for the release stops waiting.
func TestShareRequestSessionEndsWithItsContext(t *testing.T) {
	var h ShareHub
	ctx, cancel := context.WithCancel(context.Background())
	got := make(chan bool, 1)
	go func() { got <- h.requestSession(ctx, "cam", time.Minute) }()
	waitFor(t, "wants_session", func() bool { return h.counters("cam").WantsSession })
	cancel()
	select {
	case ok := <-got:
		if ok {
			t.Fatal("a cancelled request reported a release")
		}
	case <-time.After(time.Second):
		t.Fatal("the request outlived its context")
	}
	if h.counters("cam").WantsSession {
		t.Fatal("wants_session stayed on after the request ended")
	}
}

// One session at a time: the end closes at once, subscriber or not, and tells
// the subscriber so (exclusive), so the integration reconnects right away.
func TestShareExclusiveReleaseClosesFirst(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	c := newFakeShareClient(&open)
	h.setExclusive("cam", true)
	h.attach("cam", c)
	sub, _, _ := h.subscribe("cam")
	h.release("cam", c, c.Close)
	if !c.closed.Load() {
		t.Fatal("the session of a one-session camera waited for its subscriber")
	}
	ev := <-sub.ch
	if ev.Type != "ending" || !ev.Exclusive {
		t.Fatalf("got %+v, want an exclusive ending", ev)
	}
	if open.Load() != 0 {
		t.Fatalf("%d sessions still open", open.Load())
	}
}

func TestShareReleaseEndpoint(t *testing.T) {
	const token = "secret-token-for-the-test-1234567890"
	server := shareServer(token)
	srv := httptest.NewServer(server.statusHandler())
	defer srv.Close()
	post := func(auth string) int {
		req, _ := http.NewRequest(http.MethodPost, srv.URL+"/cameras/cam/release", nil)
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		return resp.StatusCode
	}
	if got := post(""); got != http.StatusUnauthorized {
		t.Fatalf("without the token: %d, want 401", got)
	}
	if got := post("Bearer " + token); got != http.StatusConflict {
		t.Fatalf("nothing waiting: %d, want 409", got)
	}
	got := make(chan bool, 1)
	go func() { got <- server.share.requestSession(context.Background(), "cam", 2*time.Second) }()
	waitFor(t, "wants_session", func() bool { return server.share.counters("cam").WantsSession })
	if code := post("Bearer " + token); code != http.StatusNoContent {
		t.Fatalf("release while waiting: %d, want 204", code)
	}
	if !<-got {
		t.Fatal("the waiting start did not see the release")
	}
}

func TestLANPathTakes33OnOneAddressOnly(t *testing.T) {
	s := newLANServer()
	target := LANTarget{LocalKey: "lk", Password: "pw", IP: "192.0.2.10", Protocol: "3.3"}
	for _, tc := range []struct {
		name  string
		hosts []string
		ok    bool
	}{
		{"the bound address, also published", []string{"192.0.2.10"}, true},
		{"only the published address: no binding", nil, false},
		{"a bound address other than the published one", []string{"192.0.2.11"}, false},
		{"two addresses", []string{"192.0.2.10", "192.0.2.11"}, false},
	} {
		target.Hosts = tc.hosts
		s.LANTargets["testdev"] = target
		if ok, reason := s.lanPath("testdev", "hd"); ok != tc.ok {
			t.Errorf("%s: ok=%v (%s), want %v", tc.name, ok, reason, tc.ok)
		}
	}
	s.LANTargets["testdev"] = LANTarget{LocalKey: "lk", Password: "pw", IP: "192.0.2.10", Protocol: "3.1"}
	if ok, _ := s.lanPath("testdev", "hd"); ok {
		t.Error("a protocol below 3.3 counted as a LAN path")
	}
}

// exclusiveStream is a stream on a 3.3 camera whose LAN sources are the real
// ones (newLANSource) on a fake monitor; started reports whether one connected.
func exclusiveStream(t *testing.T, token string) (*RTSPServer, *CameraStream, *atomic.Bool, *atomic.Int32) {
	t.Helper()
	wait := shareReleaseWait
	shareReleaseWait = 300 * time.Millisecond
	t.Cleanup(func() { shareReleaseWait = wait })
	server := newLANServer()
	server.APIToken = token
	server.LANTargets["testdev"] = LANTarget{
		LocalKey: "lk", Password: "pw", IP: "192.0.2.10", Hosts: []string{"192.0.2.10"}, Protocol: "3.3",
	}
	cs, _ := newLANStream(t, server, "hd")
	var started atomic.Bool
	cs.lanFactory = func(fwd *RTPForwarder) lanStreamer {
		src := cs.newLANSource(fwd)
		inner := src.(*exclusiveSource).LANSource
		inner.newClient = func(cfg lan.ClientConfig) lanClient {
			return &fakeLANClient{cfg: cfg, done: make(chan struct{}), start: func(f *fakeLANClient) error {
				started.Store(true)
				go f.cfg.OnVideo(testIDR)
				return nil
			}}
		}
		return src
	}
	var cloud atomic.Int32
	cs.cloudStart = func() error {
		cloud.Add(1)
		return nil
	}
	t.Cleanup(cs.stopStream)
	return server, cs, &started, &cloud
}

func TestExclusiveStartConnectsOnlyAfterTheRelease(t *testing.T) {
	server, cs, started, cloud := exclusiveStream(t, "a-token")
	released := make(chan struct{})
	go func() {
		defer close(released)
		waitFor(t, "wants_session", func() bool { return server.share.counters("testdev").WantsSession })
		if started.Load() {
			t.Error("the bridge connected before the integration released its session")
		}
		server.share.noteReleased("testdev")
	}()
	begin(cs)
	<-released
	if _, _, src := streamState(cs); !started.Load() || src == nil {
		t.Fatalf("started=%v lan=%v, want the local session after the release", started.Load(), src)
	}
	if cloud.Load() != 0 {
		t.Fatal("the cloud was started although the local session worked")
	}
	if server.share.counters("testdev").WantsSession {
		t.Fatal("wants_session stayed up after the start")
	}
}

// Never two sessions to a one-session camera: without the release the bridge
// does not connect at all, and the path does not cool down.
func TestExclusiveStartWithoutReleaseUsesTheCloud(t *testing.T) {
	server, cs, started, cloud := exclusiveStream(t, "a-token")
	begin(cs)
	if started.Load() {
		t.Fatal("connected to a one-session camera without the integration's release")
	}
	if active, _, _ := streamState(cs); cloud.Load() != 1 || !active {
		t.Fatalf("cloud=%d active=%v, want the cloud serving", cloud.Load(), active)
	}
	if server.lanCoolingDown("testdev") {
		t.Fatal("a kept session started the cool-down")
	}
	// No probe at once: it would ask Home Assistant for the session again.
	if timer := promoteTimer(cs); timer == nil || !timer.Stop() {
		t.Fatal("no promotion armed for later")
	}
}

func TestExclusiveStartWithoutTokenUsesTheCloud(t *testing.T) {
	_, cs, started, cloud := exclusiveStream(t, "")
	begin(cs)
	if started.Load() || cloud.Load() != 1 {
		t.Fatalf("started=%v cloud=%d, want the cloud without asking", started.Load(), cloud.Load())
	}
}

// A 3.4 camera keeps the old order: no request, the bridge connects at once.
func TestNonExclusiveSourceDoesNotAsk(t *testing.T) {
	server := newLANServer()
	server.APIToken = "a-token"
	cs, _ := newLANStream(t, server, "hd")
	if _, ok := cs.newLANSource(NewRTPForwarder()).(*LANSource); !ok {
		t.Fatal("a 3.5 camera got the one-session source")
	}
	if server.share.counters("testdev").WantsSession {
		t.Fatal("a 3.5 camera asked for the integration's session")
	}
}

// The camera staying silent with ours closed is logged once per camera.
func TestExclusiveSilenceNotedOnce(t *testing.T) {
	var s RTSPServer
	s.noteExclusiveSilence("cam", "Camera")
	s.noteExclusiveSilence("cam", "Camera")
	n := 0
	s.exclusiveSilence.Range(func(any, any) bool { n++; return true })
	if n != 1 {
		t.Fatalf("%d entries, want 1", n)
	}
}

func TestStatusShowsWantsSession(t *testing.T) {
	server, _ := newTestServer(t, "127.0.0.1")
	if err := server.storageManager.UpdateCamerasForUser("u", []storage.CameraInfo{{DeviceID: "cam1", UserKey: "u"}}); err != nil {
		t.Fatal(err)
	}
	wants := func() bool {
		rec := httptest.NewRecorder()
		server.statusHandler().ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/status", nil))
		var doc Status
		if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil || len(doc.Cameras) != 1 {
			t.Fatalf("GET /status: %v %s", err, rec.Body)
		}
		return doc.Cameras[0].WantsSession
	}
	if wants() {
		t.Fatal("wants_session without a waiting start")
	}
	done := make(chan bool, 1)
	go func() { done <- server.share.requestSession(context.Background(), "cam1", 2*time.Second) }()
	waitFor(t, "wants_session in /status", wants)
	server.share.noteReleased("cam1")
	<-done
	if wants() {
		t.Fatal("wants_session stayed in /status after the release")
	}
}

// A start that got the integration's session keeps asking for it until it
// ends: a slow 3.3 start otherwise saw the integration take the camera's one
// session back halfway (2026-11-04 review).
func TestWantsSessionStaysUpWhileTheStartConnects(t *testing.T) {
	var h ShareHub
	done := h.holdForStart("cam")
	if !h.counters("cam").WantsSession {
		t.Fatal("wants_session dropped while the start connects")
	}
	done()
	done() // a second call changes nothing
	if h.counters("cam").WantsSession {
		t.Fatal("wants_session still up after the start ended")
	}
	if h.cam("cam").starting != 0 {
		t.Fatalf("starting = %d, want 0", h.cam("cam").starting)
	}
}
