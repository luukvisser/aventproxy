package rtsp

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"syscall"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/storage"

	tuyalan "github.com/luukvisser/tuya-lan"
)

func TestStatusFollowsTheStream(t *testing.T) {
	server := newLANServer()
	cs, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, cs, nil)
	ids := []string{"testdev", "other"}

	if got := server.status.snapshot(ids); got[0].Path != PathIdle || got[1].Path != PathIdle {
		t.Fatalf("before any stream: %+v", got)
	}
	begin(cs)
	got := server.status.snapshot(ids)
	if got[0].Path != PathLocal || got[0].Mode != "auto" || got[0].Since == nil {
		t.Fatalf("LAN stream running: %+v", got[0])
	}
	if got[1].Path != PathIdle {
		t.Fatalf("a camera without a stream: %+v", got[1])
	}

	cs.stopStream()
	if got = server.status.snapshot(ids); got[0].Path != PathIdle || got[0].Since == nil {
		t.Fatalf("after stop: %+v", got[0])
	}
}

func TestStatusNamesTheFallback(t *testing.T) {
	server := newLANServer()
	cs, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, cs, func(context.Context) error {
		return fmt.Errorf("3.4 at 192.0.2.7: %w", tuyalan.ErrWrongKey)
	})
	begin(cs)
	got := server.status.snapshot([]string{"testdev"})[0]
	if got.Path != PathWebRTC {
		t.Fatalf("path = %q, want webrtc", got.Path)
	}
	if got.LastFallback != "local stream did not start (wrong local key), cloud signalling" || got.LastFallbackAt == nil {
		t.Fatalf("fallback = %q at %v", got.LastFallback, got.LastFallbackAt)
	}
	cs.stopStream()
}

func TestStatusEndpoint(t *testing.T) {
	server, _ := newTestServer(t, "127.0.0.1")
	if err := server.storageManager.UpdateCamerasForUser("u", []storage.CameraInfo{{DeviceID: "cam1", UserKey: "u"}}); err != nil {
		t.Fatal(err)
	}
	server.NoWebRTC = true
	h := server.statusHandler()

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/status", nil))
	var doc Status
	if err := json.Unmarshal(rec.Body.Bytes(), &doc); err != nil || rec.Code != 200 {
		t.Fatalf("GET /status: %d %v %s", rec.Code, err, rec.Body)
	}
	if doc.WebRTC || len(doc.Cameras) != 1 || doc.Cameras[0].CameraID != "cam1" || doc.Cameras[0].Path != PathIdle {
		t.Fatalf("document %+v", doc)
	}
	for _, method := range []string{http.MethodPost, http.MethodPut} {
		rec = httptest.NewRecorder()
		h.ServeHTTP(rec, httptest.NewRequest(method, "/status", nil))
		if rec.Code != http.StatusMethodNotAllowed {
			t.Fatalf("%s /status = %d", method, rec.Code)
		}
	}
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, httptest.NewRequest(http.MethodGet, "/config", nil))
	if rec.Code != http.StatusNotFound {
		t.Fatalf("GET /config = %d, want 404", rec.Code)
	}
}

// A failed LAN start's reason names the kind of failure, never the address
// the error text carries.
func TestFallbackCauseCarriesNoAddress(t *testing.T) {
	dial := &net.OpError{Op: "dial", Net: "tcp4", Addr: &net.TCPAddr{IP: net.IPv4(192, 0, 2, 7), Port: 6668}, Err: syscall.ECONNREFUSED}
	err := errors.Join(
		fmt.Errorf("3.5 at 192.0.2.7: tuyalan: dial: %w", dial),
		fmt.Errorf("3.4 at 192.0.2.7: tuyalan: 3.4 handshake: %w", tuyalan.ErrWrongKey),
		fmt.Errorf("3.4 at 192.0.2.8: %w", context.DeadlineExceeded),
		fmt.Errorf("3.4 at 192.0.2.9: %w", context.DeadlineExceeded),
	)
	got := fallbackCause(err)
	if got != "connection refused, wrong local key, timed out" {
		t.Fatalf("cause %q", got)
	}
	if strings.Contains(got, "192.0.2") {
		t.Fatalf("address in %q", got)
	}
	if got := fallbackCause(fmt.Errorf("%w: %w", errNoFirstFrame, context.DeadlineExceeded)); got != "no video" {
		t.Fatalf("no first frame gave %q", got)
	}
	if got := fallbackCause(errors.New("lan bridge stopped")); got != "error" {
		t.Fatalf("unknown error gave %q", got)
	}
}

// The size /status reports does not wait for a stream that holds its lock
// (a cloud start holds it for seconds).
func TestVideoSizeDoesNotWaitForABusyStream(t *testing.T) {
	_, stream := newTestCameraStream(t)
	stream.mutex.Lock()
	defer stream.mutex.Unlock()
	done := make(chan struct{})
	go func() { stream.videoSize(); close(done) }()
	select {
	case <-done:
	case <-time.After(time.Second):
		t.Fatal("videoSize waited for the stream's lock")
	}
}
