package rtsp

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net"
	"net/http"
	"slices"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"avent-webrtc-bridge/pkg/core"
	"avent-webrtc-bridge/pkg/lan"

	tuyalan "github.com/luukvisser/tuya-lan"
)

// The bridge's status for Home Assistant: per camera, which path its stream
// takes now (local, imm, webrtc or idle), since when, at what size, and the
// last time it fell back from one path to the other. Without it Home
// Assistant only knew the stream source it asked for, not what the bridge did
// with it. Served read-only as JSON on StatusPort, on the same addresses as
// RTSP. It carries no keys, passwords or addresses.

const (
	PathLocal  = "local"  // LAN signalling, media over the LAN
	PathIMM    = "imm"    // imm over cloud signalling
	PathWebRTC = "webrtc" // the webrtc cloud session
	PathIdle   = "idle"
)

// CameraStatus is one camera in the status document.
type CameraStatus struct {
	CameraID       string     `json:"camera_id"`
	Path           string     `json:"path"`
	Mode           string     `json:"mode,omitempty"`
	Since          *time.Time `json:"since,omitempty"`
	Width          int        `json:"width,omitempty"`
	Height         int        `json:"height,omitempty"`
	LastFallback   string     `json:"last_fallback,omitempty"`
	LastFallbackAt *time.Time `json:"last_fallback_at,omitempty"`
}

// Status is the document /status serves.
type Status struct {
	WebRTC  bool           `json:"webrtc"`
	Cameras []CameraStatus `json:"cameras"`
}

type activeStream struct {
	path  string
	since time.Time
}

type cameraBoard struct {
	active         map[*CameraStream]activeStream
	idleSince      time.Time
	lastFallback   string
	lastFallbackAt time.Time
}

// statusBoard records what the streams do. Streams call it while holding
// their own mutex, so it never takes a stream's mutex itself.
type statusBoard struct {
	mu   sync.Mutex
	cams map[string]*cameraBoard
}

func (b *statusBoard) camera(deviceID string) *cameraBoard {
	if b.cams == nil {
		b.cams = map[string]*cameraBoard{}
	}
	c, ok := b.cams[deviceID]
	if !ok {
		c = &cameraBoard{active: map[*CameraStream]activeStream{}}
		b.cams[deviceID] = c
	}
	return c
}

func (b *statusBoard) streamActive(cs *CameraStream, path string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	b.camera(cs.camera.DeviceID).active[cs] = activeStream{path: path, since: time.Now()}
}

func (b *statusBoard) streamStopped(cs *CameraStream) {
	b.mu.Lock()
	defer b.mu.Unlock()
	c := b.camera(cs.camera.DeviceID)
	if _, ok := c.active[cs]; !ok {
		return
	}
	delete(c.active, cs)
	if len(c.active) == 0 {
		c.idleSince = time.Now()
	}
}

func (b *statusBoard) fallback(deviceID, reason string) {
	b.mu.Lock()
	defer b.mu.Unlock()
	c := b.camera(deviceID)
	c.lastFallback = reason
	c.lastFallbackAt = time.Now()
}

// snapshot reports every camera in deviceIDs. A camera reports a stream a
// client watches before one that waits out its shutdown delay (which kept a
// WebRTC view reported as the local stream before it for 30 s, 05-11-2026);
// among those alike, a local stream (one LAN session per camera, so that is
// the one that matters), else the newest; with none active, idle.
func (b *statusBoard) snapshot(deviceIDs []string) []CameraStatus {
	type candidate struct {
		cs     *CameraStream
		stream activeStream
	}
	type pick struct {
		cs         *CameraStream
		candidates []candidate
		status     CameraStatus
	}
	b.mu.Lock()
	picks := make([]pick, 0, len(deviceIDs))
	for _, id := range deviceIDs {
		p := pick{status: CameraStatus{CameraID: id, Path: PathIdle}}
		if c, ok := b.cams[id]; ok {
			for cs, a := range c.active {
				p.candidates = append(p.candidates, candidate{cs, a})
			}
			if len(c.active) == 0 && !c.idleSince.IsZero() {
				since := c.idleSince
				p.status.Since = &since
			}
			if c.lastFallback != "" {
				at := c.lastFallbackAt
				p.status.LastFallback, p.status.LastFallbackAt = c.lastFallback, &at
			}
		}
		picks = append(picks, p)
	}
	b.mu.Unlock()

	// Client counts come from the forwarders' own locks, outside b.mu and
	// without the stream lock, which a start holds for seconds.
	for i := range picks {
		p := &picks[i]
		var best activeStream
		bestWatched := false
		for _, c := range p.candidates {
			watched := c.cs.forwarder != nil && c.cs.forwarder.GetClientCount() > 0
			local, bestLocal := c.stream.path == PathLocal, best.path == PathLocal
			better := p.cs == nil ||
				(watched && !bestWatched) ||
				(watched == bestWatched && local && !bestLocal) ||
				(watched == bestWatched && local == bestLocal && c.stream.since.After(best.since))
			if better {
				p.cs, best, bestWatched = c.cs, c.stream, watched
			}
		}
		if p.cs != nil {
			since := best.since
			p.status.Path, p.status.Since, p.status.Mode = best.path, &since, p.cs.mode.String()
		}
	}

	out := make([]CameraStatus, 0, len(picks))
	for _, p := range picks {
		if p.cs != nil {
			p.status.Width, p.status.Height = p.cs.videoSize()
		}
		out = append(out, p.status)
	}
	return out
}

// videoSize is the size in the SPS the stream's forwarder last saw, or 0, 0.
// The forwarder is the stream's for good, so this needs no stream lock, which
// a start holds for seconds: /status answers meanwhile.
func (cs *CameraStream) videoSize() (int, int) {
	rf := cs.forwarder
	if rf == nil {
		return 0, 0
	}
	rf.mutex.RLock()
	sps := rf.spsPacket
	rf.mutex.RUnlock()
	if sps == nil {
		return 0, 0
	}
	if w, h, ok := spsResolutionInPayload(sps.Payload); ok {
		return w, h
	}
	return 0, 0
}

// streamPath is the path an active stream takes, for the status. Callers hold
// cs.mutex.
func (cs *CameraStream) streamPath() string {
	switch cs.lan.(type) {
	case nil:
		return PathWebRTC
	case *immSource:
		return PathIMM
	}
	return PathLocal
}

// fallbackCause names what went wrong in a failed LAN start, one cause per
// kind, without the error's own text: that can carry the camera's address
// ("dial tcp4 <address>:6668"), which /status, and the sensor's history in
// Home Assistant, must not.
func fallbackCause(err error) string {
	if errors.Is(err, errNoFirstFrame) { // one attempt that ran out of time waiting for video
		return "no video"
	}
	attempts := []error{err}
	if joined, ok := err.(interface{ Unwrap() []error }); ok {
		attempts = joined.Unwrap()
	}
	var causes []string
	for _, e := range attempts {
		if cause := attemptCause(e); !slices.Contains(causes, cause) {
			causes = append(causes, cause)
		}
	}
	return strings.Join(causes, ", ")
}

func attemptCause(err error) string {
	var netErr net.Error
	switch {
	case errors.Is(err, tuyalan.ErrWrongKey):
		return "wrong local key"
	case errors.Is(err, lan.ErrEnded):
		return "the camera ended the session"
	case errors.Is(err, lan.ErrRefused):
		return "refused"
	case errors.Is(err, lan.ErrNoAnswer), errors.Is(err, tuyalan.ErrNoSignal):
		return "no answer to the stream request"
	case errors.Is(err, lan.ErrICE):
		return "ICE timeout"
	case errors.Is(err, errNoFirstFrame):
		return "no video"
	case errors.Is(err, lan.ErrMediaIdle):
		return "no media"
	case errors.Is(err, context.DeadlineExceeded), errors.As(err, &netErr) && netErr.Timeout():
		return "timed out"
	case errors.Is(err, syscall.ECONNREFUSED):
		return "connection refused"
	case errors.Is(err, syscall.EHOSTUNREACH), errors.Is(err, syscall.ENETUNREACH):
		return "unreachable"
	case errors.Is(err, io.EOF), errors.Is(err, io.ErrUnexpectedEOF), errors.Is(err, syscall.ECONNRESET):
		return "connection closed"
	}
	return "error"
}

// noteFallback records why a stream left the path it tried first.
func (cs *CameraStream) noteFallback(reason string) {
	if cs.server != nil {
		cs.server.status.fallback(cs.camera.DeviceID, reason)
	}
}

// statusDocument is what /status answers.
func (s *RTSPServer) statusDocument() Status {
	var ids []string
	if s.storageManager != nil {
		if cameras, err := s.storageManager.GetAllCameras(); err == nil {
			for _, c := range cameras {
				ids = append(ids, c.DeviceID)
			}
		}
	}
	sort.Strings(ids)
	return Status{WebRTC: !s.NoWebRTC, Cameras: s.status.snapshot(ids)}
}

func (s *RTSPServer) statusHandler() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/status", func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet {
			w.WriteHeader(http.StatusMethodNotAllowed)
			return
		}
		w.Header().Set("Content-Type", "application/json")
		_ = json.NewEncoder(w).Encode(s.statusDocument())
	})
	return mux
}

// startStatus serves /status on StatusPort at every RTSP bind host. A host it
// cannot bind is logged and skipped: the status is a diagnostic, never a
// reason for the bridge not to stream.
func (s *RTSPServer) startStatus(hosts []string) {
	if s.StatusPort <= 0 {
		return
	}
	handler := s.statusHandler()
	for _, host := range hosts {
		addr := net.JoinHostPort(host, strconv.Itoa(s.StatusPort))
		ln, err := net.Listen("tcp", addr)
		if err != nil {
			core.Logger.Warn().Msgf("Status: cannot listen on %s: %v", addr, err)
			continue
		}
		srv := &http.Server{Handler: handler, ReadHeaderTimeout: 5 * time.Second, WriteTimeout: 5 * time.Second}
		s.statusServers = append(s.statusServers, srv)
		core.Logger.Info().Msgf("Status: serving /status on %s", ln.Addr())
		go func() { _ = srv.Serve(ln) }()
	}
}

func (s *RTSPServer) stopStatus() {
	for _, srv := range s.statusServers {
		_ = srv.Close()
	}
	s.statusServers = nil
}
