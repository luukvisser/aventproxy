package rtsp

import "testing"

// unwatch drops the client newModeStream added, as a viewer leaving does:
// the stream stays active through its shutdown delay.
func unwatch(cs *CameraStream) {
	cs.mutex.Lock()
	delete(cs.clients, "s1")
	cs.mutex.Unlock()
}

func TestAnotherModeStopsAnUnwatchedStream(t *testing.T) {
	server := newLANServer()
	old, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, old, nil)
	begin(old)
	unwatch(old)
	if active, _, _ := streamState(old); !active {
		t.Fatal("the auto stream did not start")
	}

	next, _ := newModeStream(t, server, ModeWebRTC)
	if active, _, lan := streamState(old); active || lan != nil {
		t.Fatalf("unwatched auto stream still active=%v lan=%v when a webrtc stream was asked for", active, lan)
	}
	if next == old {
		t.Fatal("the webrtc request got the auto stream")
	}
}

func TestAnotherModeKeepsAWatchedStream(t *testing.T) {
	server := newLANServer()
	old, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, old, nil)
	begin(old)
	t.Cleanup(old.stopStream)

	newModeStream(t, server, ModeWebRTC)
	if active, _, _ := streamState(old); !active {
		t.Fatal("a watched auto stream was stopped for a webrtc request")
	}
}

func TestSameModeReusesTheUnwatchedStream(t *testing.T) {
	server := newLANServer()
	old, _ := newModeStream(t, server, ModeAuto)
	fakeAttempts(t, old, nil)
	begin(old)
	unwatch(old)
	t.Cleanup(old.stopStream)

	again, _ := newModeStream(t, server, ModeAuto)
	if again != old {
		t.Fatal("a second auto request did not reuse the running auto stream")
	}
	if active, _, _ := streamState(old); !active {
		t.Fatal("the auto stream was stopped for a request in its own mode")
	}
}
