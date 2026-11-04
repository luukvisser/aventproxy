package rtsp

import (
	"strings"
	"testing"

	"avent-webrtc-bridge/pkg/storage"

	"github.com/pion/rtp"
)

func talkOnDemandServer(s *RTSPServer) *RTSPServer {
	s.Talkback = true
	s.TalkbackMode = TalkbackOnDemand
	return s
}

// A stream on the LAN talks on demand as a cloud one does: the camera session
// stays local, and the clients' audio opens a talk session of its own.
func TestTalkOnDemandWhileTheStreamIsOnTheLAN(t *testing.T) {
	server := talkOnDemandServer(newLANServer())
	cs, _ := newLANStream(t, server, "hd")
	a := fakeAttempts(t, cs, nil)
	o := &talkOpener{next: func() *fakeTalk { return &fakeTalk{} }}
	cs.talk.open = o.open
	defer cs.talk.end("test over")

	begin(cs)

	if _, _, lan := streamState(cs); lan != a.lan {
		t.Fatal("the stream did not take the LAN with talk on demand")
	}
	cs.forwarder.ForwardBackchannel(&rtp.Packet{})
	eventually(t, "a talk session next to the LAN stream", func() bool { return o.count() == 1 })
	if n := a.cloudStarted.Load(); n != 0 {
		t.Fatalf("the stream's cloud session started %d times, want none: a talk has its own", n)
	}
	if _, _, lan := streamState(cs); lan != a.lan {
		t.Fatal("a talk moved the stream off the LAN")
	}
}

// Every mode but local offers a return channel with talk on demand. A local
// stream uses no MQTT and no WebRTC, so it offers none in either mode, and its
// description says so.
func TestTalkbackPerStreamMode(t *testing.T) {
	camera := &storage.CameraInfo{DeviceID: "d", DeviceName: "n"}
	server := talkOnDemandServer(NewRTSPServer(0, nil))
	backchannel := func(cs *CameraStream) bool {
		return strings.Contains(server.streamSDP(camera, "rtsp://h/n", cs.offersTalkback()), "/backchannel")
	}
	for _, mode := range []StreamMode{ModeAuto, ModeWebRTC, ModeWebRTCFirst} {
		cs := NewCameraStreamForMode(camera, "hd", mode, nil, nil, server)
		if cs.talk == nil || !cs.offersTalkback() || !backchannel(cs) {
			t.Errorf("mode %s: talker %v, offers talkback %v, backchannel %v; want all", mode, cs.talk != nil, cs.offersTalkback(), backchannel(cs))
		}
		if cs.webrtcBridge.Talkback {
			t.Errorf("mode %s: on demand the camera session must stay one-way", mode)
		}
	}
	for _, talkMode := range []string{TalkbackOnDemand, TalkbackSession} {
		server.TalkbackMode = talkMode
		local := NewCameraStreamForMode(camera, "hd", ModeLocal, nil, nil, server)
		if local.talk != nil || local.offersTalkback() || backchannel(local) {
			t.Errorf("talkback %s: a local stream has a talker %v, offers talkback %v, backchannel %v; want none",
				talkMode, local.talk != nil, local.offersTalkback(), backchannel(local))
		}
	}
}

// With WebRTC over the LAN only, a talk's own session keeps to the LAN as
// the stream's does: no STUN/TURN, the camera's own addresses.
func TestTalkSessionKeepsWebRTCOnTheLAN(t *testing.T) {
	server := talkOnDemandServer(NewRTSPServer(0, nil))
	server.WebRTCLANOnly = true
	server.CameraLANHosts = map[string][]string{"d": {"192.0.2.10"}}
	cs := NewCameraStream(&storage.CameraInfo{DeviceID: "d", DeviceName: "n"}, "hd", nil, nil, server)
	session, err := cs.openTalkSession(func(error) {})
	if err != nil {
		t.Fatal(err)
	}
	wb := session.(talkBridge).WebRTCBridge
	defer wb.cancel()
	if !wb.Talkback || !wb.LANOnly || len(wb.LANHosts) != 1 || wb.LANHosts[0] != "192.0.2.10" {
		t.Fatalf("talk session: talkback %v, LAN only %v, hosts %v; want two-way on the camera's LAN address",
			wb.Talkback, wb.LANOnly, wb.LANHosts)
	}
}
