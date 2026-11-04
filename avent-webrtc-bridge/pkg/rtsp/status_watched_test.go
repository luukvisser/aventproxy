package rtsp

import (
	"net"
	"testing"

	"avent-webrtc-bridge/pkg/storage"
)

func TestStatusPrefersTheWatchedStream(t *testing.T) {
	server := NewRTSPServer(0, nil)
	camera := &storage.CameraInfo{DeviceID: "testdev", DeviceName: "Test Cam"}
	user := &storage.UserSession{UserKey: "user1"}
	local := NewCameraStreamForMode(camera, "hd", ModeAuto, user, nil, server)
	cloud := NewCameraStreamForMode(camera, "hd", ModeWebRTC, user, nil, server)
	server.status.streamActive(local, PathLocal)
	server.status.streamActive(cloud, PathWebRTC)

	if got := server.status.snapshot([]string{"testdev"})[0]; got.Path != PathLocal {
		t.Fatalf("nobody watching: path = %q, want local first", got.Path)
	}

	clientConn, serverConn := net.Pipe()
	t.Cleanup(func() {
		clientConn.Close()
		serverConn.Close()
	})
	if err := cloud.forwarder.AddTCPClient("c1", serverConn, 0, 1, 255); err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { cloud.forwarder.RemoveClient("c1") })
	if got := server.status.snapshot([]string{"testdev"})[0]; got.Path != PathWebRTC || got.Mode != "webrtc" {
		t.Fatalf("webrtc watched, local in its shutdown delay: path = %q mode = %q, want webrtc", got.Path, got.Mode)
	}
}
