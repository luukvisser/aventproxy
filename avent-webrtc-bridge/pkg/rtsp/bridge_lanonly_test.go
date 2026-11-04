package rtsp

import (
	"strings"
	"testing"

	"avent-webrtc-bridge/pkg/storage"
	"avent-webrtc-bridge/pkg/tuya"
)

// Placeholder servers and documentation addresses only; nothing here is real.
var testICEServers = []tuya.ICEServer{
	{Urls: "stun:stun.example.invalid:3478"},
	{Urls: "turn:turn.example.invalid:3478", Username: "u", Credential: "fake"},
}

func TestPeerConfiguration_LANOnlyHasNoICEServers(t *testing.T) {
	conf, err := peerConfiguration(testICEServers, true)
	if err != nil {
		t.Fatal(err)
	}
	if len(conf.ICEServers) != 0 {
		t.Fatalf("LAN-only config has %d ICE servers, want none", len(conf.ICEServers))
	}

	api, err := newWebRTCAPI(true)
	if err != nil {
		t.Fatal(err)
	}
	pc, err := api.NewPeerConnection(conf)
	if err != nil {
		t.Fatal(err)
	}
	defer pc.Close()
	if n := len(pc.GetConfiguration().ICEServers); n != 0 {
		t.Fatalf("peer connection has %d ICE servers", n)
	}
}

func TestPeerConfiguration_DefaultKeepsTuyaICEServers(t *testing.T) {
	conf, err := peerConfiguration(testICEServers, false)
	if err != nil {
		t.Fatal(err)
	}
	if len(conf.ICEServers) != 2 || conf.ICEServers[1].Username != "u" ||
		conf.ICEServers[1].URLs[0] != "turn:turn.example.invalid:3478" {
		t.Fatalf("ICE servers = %+v", conf.ICEServers)
	}
}

const (
	hostLAN   = "a=candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host"
	hostULA   = "a=candidate:2 1 udp 2130706431 fd12:3456::1 40000 typ host"
	srflx     = "a=candidate:3 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.20 rport 40000"
	relay     = "a=candidate:4 1 udp 16777215 203.0.113.50 3478 typ relay raddr 203.0.113.9 rport 51000"
	hostWorld = "a=candidate:5 1 udp 2130706431 198.51.100.7 40000 typ host"
)

func newTestBridge(lanOnly bool) *WebRTCBridge {
	wb := NewWebRTCBridge(&storage.CameraInfo{DeviceID: "dev", DeviceName: "cam"}, "hd", nil, nil, nil)
	wb.LANOnly = lanOnly
	return wb
}

func TestHandleRemoteCandidate_LANOnlyKeepsOnlyLANHosts(t *testing.T) {
	wb := newTestBridge(true)
	for _, c := range []string{hostLAN, srflx, relay, hostWorld, hostULA} {
		wb.handleRemoteCandidate(c)
	}
	if len(wb.pendingCandidates) != 2 {
		t.Fatalf("buffered %d candidates, want the 2 LAN host ones: %+v", len(wb.pendingCandidates), wb.pendingCandidates)
	}
	if !strings.Contains(wb.pendingCandidates[0].Candidate, "192.168.1.20") ||
		!strings.Contains(wb.pendingCandidates[1].Candidate, "fd12:3456::1") {
		t.Fatalf("wrong candidates kept: %+v", wb.pendingCandidates)
	}
	if wb.droppedCandidates != 3 {
		t.Fatalf("dropped %d, want 3", wb.droppedCandidates)
	}
}

func TestHandleRemoteCandidate_DefaultKeepsEverything(t *testing.T) {
	wb := newTestBridge(false)
	for _, c := range []string{hostLAN, srflx, relay, hostWorld} {
		wb.handleRemoteCandidate(c)
	}
	if len(wb.pendingCandidates) != 4 || wb.droppedCandidates != 0 {
		t.Fatalf("buffered %d, dropped %d; want 4, 0", len(wb.pendingCandidates), wb.droppedCandidates)
	}
	if wb.pendingCandidates[1].Candidate != strings.TrimPrefix(srflx, "a=") {
		t.Fatalf("candidate changed: %q", wb.pendingCandidates[1].Candidate)
	}
}

func TestRemoteAnswerSDP(t *testing.T) {
	answer := strings.Join([]string{"v=0", "m=video 9 UDP/TLS/RTP/SAVPF 96", hostLAN, srflx, relay, hostWorld, ""}, "\r\n")

	if got := newTestBridge(false).remoteAnswerSDP(answer); got != answer {
		t.Fatalf("default bridge changed the answer:\n%s", got)
	}

	got := newTestBridge(true).remoteAnswerSDP(answer)
	if !strings.Contains(got, hostLAN+"\r\n") {
		t.Fatalf("LAN host candidate lost:\n%s", got)
	}
	for _, gone := range []string{srflx, relay, hostWorld} {
		if strings.Contains(got, gone) {
			t.Fatalf("%q survived:\n%s", gone, got)
		}
	}
}

func TestServerPassesWebRTCLANOnlyToTheBridge(t *testing.T) {
	server := NewRTSPServer(0, nil)
	server.WebRTCLANOnly = true
	server.CameraLANHosts = map[string][]string{"dev": {"192.168.1.20"}}
	camera := &storage.CameraInfo{DeviceID: "dev", DeviceName: "cam"}
	stream := NewCameraStreamForMode(camera, "hd", ModeWebRTC, nil, nil, server)
	if !stream.webrtcBridge.LANOnly || len(stream.webrtcBridge.LANHosts) != 1 {
		t.Fatalf("new stream's WebRTC bridge: LAN-only %v, hosts %v", stream.webrtcBridge.LANOnly, stream.webrtcBridge.LANHosts)
	}
	stream.mutex.Lock()
	stream.replaceBridge()
	stream.mutex.Unlock()
	if !stream.webrtcBridge.LANOnly || len(stream.webrtcBridge.LANHosts) != 1 {
		t.Fatal("a replaced WebRTC bridge lost the LAN-only settings")
	}
}

// A host candidate on a public address is kept on the camera's one known LAN
// address (an SCD973-family unit on protocol 3.3 put its public address in
// its host candidate, 02-11-2026).
func TestHandleRemoteCandidate_LANOnlyRewritesOntoTheCamerasAddress(t *testing.T) {
	wb := newTestBridge(true)
	wb.LANHosts = []string{"192.168.1.20"}
	wb.handleRemoteCandidate(hostWorld)
	if len(wb.pendingCandidates) != 1 || !strings.Contains(wb.pendingCandidates[0].Candidate, "192.168.1.20 40000 typ host") {
		t.Fatalf("buffered %+v, want the candidate on the camera's address", wb.pendingCandidates)
	}
}
