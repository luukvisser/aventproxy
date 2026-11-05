package rtsp

import (
	"encoding/json"
	"io"
	"net"
	"strings"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/core"
	"avent-webrtc-bridge/pkg/lan"

	"github.com/pion/rtp"
	"github.com/rs/zerolog"
)

// watchHarness is a forwarder with one TCP client on the G.711 track, a fake
// clock for the audio accounting and the log captured.
type watchHarness struct {
	t   *testing.T
	rf  *RTPForwarder
	now time.Time
	log *syncBuffer
}

// String is the whole log; syncBuffer is lan_promote_test.go's.
func (b *syncBuffer) String() string {
	b.mu.Lock()
	defer b.mu.Unlock()
	return b.buf.String()
}

func newWatchHarness(t *testing.T) *watchHarness {
	t.Helper()
	h := &watchHarness{t: t, now: time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC), log: &syncBuffer{}}
	oldLog, oldClock := core.Logger, audioNow
	core.Logger = zerolog.New(h.log).Level(zerolog.InfoLevel)
	audioNow = func() time.Time { return h.now }
	t.Cleanup(func() { core.Logger, audioNow = oldLog, oldClock })
	h.rf = NewRTPForwarder()
	h.rf.AACLabel = "Cam"
	return h
}

// addClient registers a TCP client that reads and discards what it gets.
func (h *watchHarness) addClient(id string, audioChannel byte) {
	h.t.Helper()
	server, client := net.Pipe()
	go func() { _, _ = io.Copy(io.Discard, client) }()
	h.t.Cleanup(func() { server.Close(); client.Close() })
	if err := h.rf.AddTCPClient(id, server, 0, audioChannel, 4); err != nil {
		h.t.Fatal(err)
	}
}

// audio sends one camera audio packet through the same calls the bridges make.
func (h *watchHarness) audio() {
	h.rf.NoteCameraAudio(false)
	h.rf.ForwardAudioPacket(&rtp.Packet{Header: rtp.Header{Version: 2}, Payload: []byte{0xFF, 0xFF}})
}

// check runs the watchdog at the fake clock.
func (h *watchHarness) check() {
	h.rf.mutex.Lock()
	h.rf.checkAudioLocked(h.now)
	h.rf.mutex.Unlock()
}

func (h *watchHarness) advance(d time.Duration) { h.now = h.now.Add(d) }

func (h *watchHarness) lines() []string {
	var out []string
	for _, l := range strings.Split(strings.TrimSpace(h.log.String()), "\n") {
		if l != "" {
			out = append(out, l)
		}
	}
	return out
}

func TestAudioStageCountsAndMeasuresGaps(t *testing.T) {
	var s audioStage
	t0 := time.Date(2026, 11, 4, 12, 0, 0, 0, time.UTC)
	for _, ms := range []int{0, 20, 40, 340, 360} {
		s.note(t0.Add(time.Duration(ms) * time.Millisecond))
	}
	if s.count != 5 || s.gapMax != 300*time.Millisecond {
		t.Fatalf("count %d, longest gap %s; want 5, 300ms", s.count, s.gapMax)
	}
	// The open gap counts too.
	if got := s.longest(t0.Add(10 * time.Second)); got != 10*time.Second-360*time.Millisecond {
		t.Fatalf("longest with an open gap = %s", got)
	}
	st := s.status(t0.Add(time.Second))
	if st.Count != 5 || st.LastAgeS == nil || *st.LastAgeS != 0.6 || st.Silent {
		t.Fatalf("status = %+v", st)
	}
	if st := (&audioStage{}).status(t0); st.LastAgeS != nil || st.Count != 0 {
		t.Fatalf("a stage without audio reports an age: %+v", st)
	}
}

func TestAudioWatchNamesSilentCameraOnceAndLogsResume(t *testing.T) {
	h := newWatchHarness(t)
	h.addClient("aaaaaaaabbbbbbbb", 2)
	h.audio()
	h.check()
	// Ten seconds in which every second a check runs and no audio comes.
	for i := 0; i < 10; i++ {
		h.advance(time.Second)
		h.check()
	}
	lines := h.lines()
	if len(lines) != 1 || !strings.Contains(lines[0], `"level":"warn"`) ||
		!strings.Contains(lines[0], "Audio of Cam: camera sends no audio for 6s while video flows (1 frames so far, 0 ignored)") {
		t.Fatalf("log after a silent camera:\n%s", strings.Join(lines, "\n"))
	}
	// The client's G.711 is not blamed for the camera.
	if strings.Contains(h.log.String(), "no G.711") {
		t.Fatalf("downstream stage blamed for the camera:\n%s", h.log.String())
	}

	h.advance(time.Second)
	h.audio()
	lines = h.lines()
	if len(lines) != 2 || !strings.Contains(lines[1], `"level":"info"`) ||
		!strings.Contains(lines[1], "Audio of Cam: camera audio is back after 11s") {
		t.Fatalf("log after the camera came back:\n%s", strings.Join(lines, "\n"))
	}
	// A second silence is a new period with its own warning.
	h.advance(6 * time.Second)
	h.check()
	if n := len(h.lines()); n != 3 {
		t.Fatalf("second silent period logged %d lines in all, want 3", n)
	}
}

func TestAudioWatchQuietDuringShortGaps(t *testing.T) {
	h := newWatchHarness(t)
	h.addClient("aaaaaaaabbbbbbbb", 2)
	// Audio with gaps up to 4 s, longer than the 250 ms pause the AAC clock
	// expects, shorter than audioSilentAfter, checked every second.
	for _, gap := range []time.Duration{20 * time.Millisecond, 300 * time.Millisecond, 4 * time.Second, 20 * time.Millisecond} {
		h.audio()
		for waited := time.Duration(0); waited < gap; waited += time.Second {
			h.advance(min(time.Second, gap-waited))
			h.check()
		}
	}
	if lines := h.lines(); len(lines) != 0 {
		t.Fatalf("normal gaps logged:\n%s", strings.Join(lines, "\n"))
	}
}

func TestAudioWatchNamesClientWithoutG711(t *testing.T) {
	h := newWatchHarness(t)
	h.addClient("aaaaaaaabbbbbbbb", 2)
	h.addClient("ccccccccdddddddd", 2)
	h.audio()
	h.check()
	for i := 0; i < 8; i++ {
		h.advance(time.Second)
		h.audio()
		h.check()
	}
	// Both received, so no warning yet.
	if lines := h.lines(); len(lines) != 0 {
		t.Fatalf("log with both clients served:\n%s", strings.Join(lines, "\n"))
	}
	// Now only the first client's stage keeps moving (the other's writes
	// stop counting, as when they fail).
	h.rf.mutex.Lock()
	for i := 0; i < 8; i++ {
		h.advance(time.Second)
		h.rf.audio.camera.note(h.now)
		h.rf.clients["aaaaaaaabbbbbbbb"].g711.note(h.now)
		h.rf.checkAudioLocked(h.now)
	}
	h.rf.mutex.Unlock()
	lines := h.lines()
	if len(lines) != 1 || !strings.Contains(lines[0], "no G.711 to client cccccccc for 6s while the camera's audio flows") {
		t.Fatalf("log with one client starved:\n%s", strings.Join(lines, "\n"))
	}
}

func TestAudioWatchNamesSilentAACCopyAndNotItsClients(t *testing.T) {
	h := newWatchHarness(t)
	h.rf.AACFFmpeg = "/nonexistent/ffmpeg"
	h.addClient("aaaaaaaabbbbbbbb", 2)
	if err := h.rf.SetupAACTrack("aaaaaaaabbbbbbbb", 6, 0); err != nil {
		t.Fatal(err)
	}
	// An encoder is "running" (the watchdog only looks at rf.aac), but
	// produces nothing.
	h.rf.aac = &aacEncoder{done: make(chan struct{}), queue: make(chan []byte, 1), stdin: nopWriteCloser{}}
	h.audio()
	h.check()
	for i := 0; i < 10; i++ {
		h.advance(time.Second)
		h.audio()
		h.check()
	}
	lines := h.lines()
	if len(lines) != 1 || !strings.Contains(lines[0], "AAC copy silent for 6s while the camera's audio flows") {
		t.Fatalf("log with a silent encoder:\n%s", strings.Join(lines, "\n"))
	}
	// Frames start again: one resume line, for the copy.
	h.rf.mutex.Lock()
	gen := h.rf.aacGen
	h.rf.mutex.Unlock()
	h.rf.forwardAAC(gen, []byte{1, 2, 3})
	lines = h.lines()
	if len(lines) != 2 || !strings.Contains(lines[1], "AAC copy is back after 10s") {
		t.Fatalf("log after the encoder came back:\n%s", strings.Join(lines, "\n"))
	}
}

type nopWriteCloser struct{}

func (nopWriteCloser) Write(p []byte) (int, error) { return len(p), nil }
func (nopWriteCloser) Close() error                { return nil }

func TestAudioWatchIgnoresAClientWithoutTheTrack(t *testing.T) {
	h := newWatchHarness(t)
	// Audio channel equal to the video channel: the client did not set up G.711.
	h.addClient("aaaaaaaabbbbbbbb", 0)
	h.audio()
	h.check()
	h.advance(8 * time.Second)
	h.audio()
	h.check()
	if lines := h.lines(); len(lines) != 0 {
		t.Fatalf("a client without the track was blamed:\n%s", strings.Join(lines, "\n"))
	}
	st := h.rf.audioStatus(h.now)
	if len(st.Clients) != 1 || st.Clients[0].G711 != nil || st.Clients[0].AAC != nil {
		t.Fatalf("status shows tracks the client never set up: %+v", st.Clients)
	}
}

func TestAudioWatchCountsLANAudioStages(t *testing.T) {
	h := newWatchHarness(t)
	h.addClient("aaaaaaaabbbbbbbb", 2)
	src := NewLANSource("dev", "cam", "uid", LANTarget{}, h.rf)
	src.onAudio(&lan.AudioFrame{SampleRate: 16000, Channels: 1, Samples: make([]byte, 8)})
	h.advance(500 * time.Millisecond)
	src.onAudio(&lan.AudioFrame{SampleRate: 16000, Channels: 1, Samples: make([]byte, 8)})
	src.onAudio(&lan.AudioFrame{SampleRate: 44100, Channels: 2, Samples: make([]byte, 4)}) // cannot be converted
	st := h.rf.audioStatus(h.now)
	if st.Camera.Count != 3 || st.CameraIgnored != 1 || st.G711Made != 2 {
		t.Fatalf("camera %d frames, %d ignored, %d G.711 made; want 3, 1, 2", st.Camera.Count, st.CameraIgnored, st.G711Made)
	}
	if len(st.Clients) != 1 || st.Clients[0].G711 == nil || st.Clients[0].G711.Count != 2 || st.Clients[0].AAC != nil {
		t.Fatalf("client counts: %+v", st.Clients)
	}
	if st.Clients[0].Session != "aaaaaaaa" {
		t.Fatalf("session shown as %q, want the first 8 characters", st.Clients[0].Session)
	}
	if st.AACCopy != nil || st.AACRunning {
		t.Fatalf("AAC copy reported without an ffmpeg: %+v", st)
	}
	if _, err := json.Marshal(st); err != nil {
		t.Fatal(err)
	}
}

func TestAudioSummaryOnStop(t *testing.T) {
	h := newWatchHarness(t)
	h.addClient("aaaaaaaabbbbbbbb", 2)
	h.audio()
	h.check()
	h.advance(300 * time.Millisecond)
	h.audio()
	h.advance(7 * time.Second)
	h.check() // silent now
	h.rf.Stop()
	h.rf.Stop() // a second Stop says nothing more
	var summaries []string
	for _, l := range h.lines() {
		if strings.Contains(l, "Audio of Cam over") {
			summaries = append(summaries, l)
		}
	}
	if len(summaries) != 1 {
		t.Fatalf("%d summary lines:\n%s", len(summaries), h.log.String())
	}
	for _, want := range []string{
		"camera 2 frames (0 ignored, longest gap 7s)", "2 G.711 packets made", "AAC copy off",
		"aaaaaaaa G.711 2 (gap 7s), AAC none", "silent at the end: camera",
	} {
		if !strings.Contains(summaries[0], want) {
			t.Errorf("summary lacks %q:\n%s", want, summaries[0])
		}
	}
}

func TestAudioSummaryNotLoggedForAnUnusedForwarder(t *testing.T) {
	h := newWatchHarness(t)
	h.rf.Stop()
	if lines := h.lines(); len(lines) != 0 {
		t.Fatalf("an unused forwarder logged:\n%s", strings.Join(lines, "\n"))
	}
}

func TestStatusCarriesAudioOfTheRunningStream(t *testing.T) {
	server := newLANServer()
	stream, _ := newLANStream(t, server, "hd")
	fakeAttempts(t, stream, nil)
	begin(stream)
	t.Cleanup(stream.stopStream)
	stream.forwarder.NoteCameraAudio(false)
	got := server.status.snapshot([]string{"testdev"})[0]
	if got.Audio == nil || got.Audio.Camera.Count != 1 {
		t.Fatalf("status without the audio counts: %+v", got)
	}
	raw, err := json.Marshal(got)
	if err != nil || !strings.Contains(string(raw), `"audio":{"camera":{"count":1,"last_age_s":`) {
		t.Fatalf("status JSON: %s %v", raw, err)
	}
}
