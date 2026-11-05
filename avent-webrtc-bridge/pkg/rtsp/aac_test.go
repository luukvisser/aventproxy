package rtsp

import (
	"bytes"
	"encoding/binary"
	"io"
	"net"
	"strings"
	"sync"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/storage"

	"github.com/pion/rtp"
)

// adtsFrame builds one ADTS frame (no CRC) around payload.
func adtsFrame(payload []byte) []byte {
	n := len(payload) + 7
	return append([]byte{
		0xFF, 0xF1, // sync, MPEG-4, layer 0, no CRC
		0x60,                    // AAC-LC, 16 kHz, private 0, channel config high bit 0
		0x40 | byte(n>>11)&0x03, // channel config 1, frame length bits 12-11
		byte(n >> 3),
		byte(n&0x07)<<5 | 0x1F,
		0xFC,
	}, payload...)
}

func TestReadADTSSplitsFrames(t *testing.T) {
	stream := append([]byte{0x00, 0x12}, adtsFrame([]byte("first"))...)
	stream = append(stream, adtsFrame(bytes.Repeat([]byte{0xAB}, 300))...)
	var got [][]byte
	readADTS(bytes.NewReader(stream), func(f []byte) { got = append(got, f) })
	if len(got) != 2 || string(got[0]) != "first" || len(got[1]) != 300 {
		t.Fatalf("frames %q", got)
	}
}

func TestAACPacketCarriesOneAUHeader(t *testing.T) {
	frame := bytes.Repeat([]byte{1}, 300)
	p := aacPacket(frame, 7, 1024, aacSSRC)
	if p.PayloadType != 97 || !p.Marker || p.SequenceNumber != 7 || p.Timestamp != 1024 {
		t.Fatalf("header %+v", p.Header)
	}
	if binary.BigEndian.Uint16(p.Payload[0:2]) != 16 {
		t.Fatalf("AU-headers-length %d, want 16 bits", binary.BigEndian.Uint16(p.Payload[0:2]))
	}
	au := binary.BigEndian.Uint16(p.Payload[2:4])
	if au>>3 != 300 || au&0x07 != 0 {
		t.Fatalf("AU header size %d index %d", au>>3, au&0x07)
	}
	if !bytes.Equal(p.Payload[4:], frame) {
		t.Fatal("frame not copied after the header")
	}
}

func TestAACClockCountsFramesAndCatchesUpAfterAPause(t *testing.T) {
	var c aacClock
	start := time.Unix(1000, 0)
	if c.next(start) != 0 {
		t.Fatal("first frame not at 0")
	}
	// Frames arriving faster than real time keep their 1024-tick spacing.
	if c.next(start.Add(10*time.Millisecond)) != 1024 || c.next(start.Add(20*time.Millisecond)) != 2048 {
		t.Fatal("frames not 1024 ticks apart")
	}
	// After 2 s without audio the next frame is stamped at the wall clock.
	if ts := c.next(start.Add(2 * time.Second)); ts != 32000 {
		t.Fatalf("after a pause ts %d, want 32000", ts)
	}
}

func TestSDPPutsAACBeforeG711(t *testing.T) {
	s := &RTSPServer{AACFFmpeg: "/usr/bin/ffmpeg"}
	sdp := s.generateSDP(&storage.CameraInfo{}, "rtsp://h/cam")
	aac := strings.Index(sdp, "MPEG4-GENERIC/16000/1")
	pcmu := strings.Index(sdp, "PCMU/8000")
	if aac < 0 || pcmu < 0 || aac > pcmu {
		t.Fatalf("AAC must come before G.711:\n%s", sdp)
	}
	if !strings.Contains(sdp, "a=control:rtsp://h/cam/aac\r\n") || !strings.Contains(sdp, "config=1408") {
		t.Fatalf("AAC track incomplete:\n%s", sdp)
	}

	s.AACFFmpeg = ""
	if sdp := s.generateSDP(&storage.CameraInfo{}, "rtsp://h/cam"); strings.Contains(sdp, "MPEG4-GENERIC") {
		t.Fatalf("AAC offered without ffmpeg:\n%s", sdp)
	}
}

// The first AAC packet is the silent frame, right after the first video
// packet, before the encoder has produced anything.
func TestSilentAACFrameFollowsTheFirstVideo(t *testing.T) {
	conn, peer := net.Pipe()
	defer conn.Close()
	defer peer.Close()
	d := newDrain(peer)

	rf := NewRTPForwarder()
	rf.AACFFmpeg = "/nonexistent/ffmpeg" // never started: no G.711 is sent
	if err := rf.AddTCPClient("s1", conn, 0, 2, 0); err != nil {
		t.Fatal(err)
	}
	if err := rf.SetupAACTrack("s1", 6, 0); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 3; i++ {
		rf.ForwardVideoPacket(&rtp.Packet{Header: rtp.Header{SequenceNumber: uint16(i), Marker: true}, Payload: []byte{0x41, 0x9a}})
	}
	deadline := time.Now().Add(time.Second)
	for (d.count(0) < 3 || d.count(6) < 1) && time.Now().Before(deadline) {
		time.Sleep(10 * time.Millisecond)
	}
	d.mu.Lock()
	defer d.mu.Unlock()
	if len(d.packets[6]) != 1 {
		t.Fatalf("%d AAC packets, want exactly one silent frame", len(d.packets[6]))
	}
	if got := d.packets[6][0].Payload[4:]; !bytes.Equal(got, aacSilentFrame) {
		t.Fatalf("first AAC frame %x, want the silent frame", got)
	}
}

func TestAACClockResyncsAfterTheSilentFrame(t *testing.T) {
	var c aacClock
	start := time.Unix(1000, 0)
	c.next(start)
	c.resync = true
	// The encoder's first frame 300 ms later is stamped at the wall clock,
	// not 1024 ticks after the silent frame.
	if ts := c.next(start.Add(300 * time.Millisecond)); ts != 4800 {
		t.Fatalf("ts %d, want 4800", ts)
	}
	if ts := c.next(start.Add(310 * time.Millisecond)); ts != 4800+1024 {
		t.Fatalf("ts %d, want counting on", ts)
	}
}

func TestAACTrackURLKeepsCameraAndMode(t *testing.T) {
	req := parseStreamPath("rtsp://h:8554/Cam/local/aac")
	if req.path != "/Cam" || req.mode != ModeLocal {
		t.Fatalf("got %+v", req)
	}
}

// Only a URL ending in /aac is the AAC track, so a camera whose name starts
// with "aac" keeps its video and G.711 tracks.
func TestAACTrackIsTheLastSegmentOnly(t *testing.T) {
	for url, want := range map[string]string{
		"rtsp://h:8554/Cam/local/aac":     "aac",
		"rtsp://h:8554/aacroom/video":     "video",
		"rtsp://h:8554/aacroom/aac/":      "aac",
		"rtsp://h:8554/aacroom":           "aacroom",
		"rtsp://u:p@h:8554/Cam/audio?x=1": "audio",
	} {
		if got := lastPathSegment(url); got != want {
			t.Errorf("%s: %q, want %q", url, got, want)
		}
	}
}

// drain records the interleaved RTP packets arriving on conn, per channel.
type drain struct {
	mu      sync.Mutex
	packets map[byte][]*rtp.Packet
}

func newDrain(conn net.Conn) *drain {
	d := &drain{packets: map[byte][]*rtp.Packet{}}
	go func() {
		head := make([]byte, 4)
		for {
			if _, err := io.ReadFull(conn, head); err != nil {
				return
			}
			body := make([]byte, binary.BigEndian.Uint16(head[2:4]))
			if _, err := io.ReadFull(conn, body); err != nil {
				return
			}
			p := &rtp.Packet{}
			if p.Unmarshal(body) == nil {
				d.mu.Lock()
				d.packets[head[1]] = append(d.packets[head[1]], p)
				d.mu.Unlock()
			}
		}
	}()
	return d
}

func (d *drain) count(channel byte) int {
	d.mu.Lock()
	defer d.mu.Unlock()
	return len(d.packets[channel])
}

func g711Silence(seq uint16) *rtp.Packet {
	return &rtp.Packet{Header: rtp.Header{Version: 2, PayloadType: 0, SequenceNumber: seq}, Payload: bytes.Repeat([]byte{0xFF}, 160)}
}

// A client that set up video and AAC but not G.711 has audio channel 0, the
// video's: G.711 must not land there.
func TestG711SkipsAClientWithoutTheTrack(t *testing.T) {
	conn, peer := net.Pipe()
	defer conn.Close()
	defer peer.Close()
	d := newDrain(peer)

	rf := NewRTPForwarder()
	if err := rf.AddTCPClient("s1", conn, 0, 0, 0); err != nil {
		t.Fatal(err)
	}
	for i := 0; i < 5; i++ {
		rf.ForwardAudioPacket(g711Silence(uint16(i)))
	}
	time.Sleep(50 * time.Millisecond)
	if n := d.count(0); n != 0 {
		t.Fatalf("%d G.711 packets on the video channel", n)
	}
}

func TestAACReachesOnlyClientsWithTheTrack(t *testing.T) {
	ffmpeg := FindFFmpeg()
	if ffmpeg == "" {
		t.Skip("no ffmpeg with an AAC encoder")
	}
	withConn, withPeer := net.Pipe()
	withoutConn, withoutPeer := net.Pipe()
	defer withConn.Close()
	defer withPeer.Close()
	defer withoutConn.Close()
	defer withoutPeer.Close()
	with, without := newDrain(withPeer), newDrain(withoutPeer)

	rf := NewRTPForwarder()
	rf.AACFFmpeg = ffmpeg
	if err := rf.AddTCPClient("with", withConn, 0, 2, 0); err != nil {
		t.Fatal(err)
	}
	if err := rf.SetupAACTrack("with", 6, 0); err != nil {
		t.Fatal(err)
	}
	if err := rf.AddTCPClient("without", withoutConn, 0, 2, 0); err != nil {
		t.Fatal(err)
	}

	// Two seconds of G.711 silence, in real time. Home Assistant drops an
	// audio track without a packet in its first 20 (about a second of
	// video), so the first AAC must come well within that.
	start := time.Now()
	var first time.Duration
	for i := 0; i < 100; i++ {
		rf.ForwardAudioPacket(g711Silence(uint16(i)))
		time.Sleep(20 * time.Millisecond)
		if first == 0 && with.count(6) > 0 {
			first = time.Since(start)
		}
	}
	if first == 0 || first > 600*time.Millisecond {
		t.Fatalf("first AAC packet after %v, want under 600 ms", first)
	}
	deadline := time.Now().Add(5 * time.Second)
	for with.count(6) < 10 && time.Now().Before(deadline) {
		time.Sleep(50 * time.Millisecond)
	}
	if n := with.count(6); n < 10 {
		t.Fatalf("%d AAC packets, want at least 10 for 2 s of audio", n)
	}
	with.mu.Lock()
	pkt := with.packets[6][0]
	with.mu.Unlock()
	if pkt.PayloadType != 97 || len(pkt.Payload) < 5 {
		t.Fatalf("not an AAC packet: %+v", pkt.Header)
	}
	if without.count(6) != 0 {
		t.Fatal("AAC sent to a client without the track")
	}
	if with.count(2) == 0 || without.count(2) == 0 {
		t.Fatal("G.711 no longer reaches its clients")
	}

	// The encoder stops with the last client that has the track.
	rf.RemoveClient("with")
	rf.mutex.RLock()
	running := rf.aac != nil
	rf.mutex.RUnlock()
	if running {
		t.Fatal("encoder still running without an AAC client")
	}
	rf.Stop()
}
