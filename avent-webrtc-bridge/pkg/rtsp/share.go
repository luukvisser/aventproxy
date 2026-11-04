package rtsp

import (
	"context"
	"crypto/subtle"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"regexp"
	"strings"
	"sync"
	"time"

	"avent-webrtc-bridge/pkg/core"

	tuyalan "github.com/luukvisser/tuya-lan"
)

// The integration's "shared" mode: while a local stream runs, the bridge's LAN
// session to the camera also carries the integration's control traffic, so the
// camera sees one session from this house instead of two. A camera accepts
// three at most (an SCD951, measured 02-11-2026), and its parent unit may need
// one of them.
//
// The integration subscribes to the data points the camera pushes on the
// bridge's session (GET /cameras/<id>/events, Server-Sent Events) and sends its
// commands through it (POST /cameras/<id>/dps). Both need the bearer token the
// integration writes into the bridge config (api_token); without a token in the
// config the endpoints do not exist.
//
// The order that keeps every push: the integration subscribes and waits for the
// "hello" event, which carries a fresh query of all points and so proves the
// session delivers data, before it closes its own session; and it reopens its
// own before it lets go. When a stream ends while the integration is
// subscribed, the bridge announces it ("ending") and keeps the camera session
// open until the integration has unsubscribed, at most shareLinger; a new
// local start for that camera closes such a lingering session first, so this
// side never holds more than two sessions to a camera.
//
// A camera that serves one session at a time (protocol 3.3, LANTarget.Exclusive)
// needs the opposite order: before the bridge connects it asks for the session
// (wants_session in /status), the integration closes its own and confirms
// (POST /cameras/<id>/release), and only then does the bridge connect; without
// that confirmation within shareReleaseWait the bridge does not connect at all
// and streams over the cloud. At the end the bridge closes first: it announces
// "ending" with exclusive set and closes its session at once, and the
// integration then reconnects. So this side never holds two sessions to it.

const (
	// shareLinger bounds how long an ended stream keeps its camera session for
	// a subscriber that is taking its own session back.
	shareLinger = 15 * time.Second
	// shareBuffer is how many events a subscriber may fall behind before it
	// is cut off; the integration then takes its own session back.
	shareBuffer = 64
	// sharePing keeps an idle event stream visibly alive.
	sharePing = 10 * time.Second
	// shareQueryTimeout and shareCommandTimeout bound one exchange with the
	// camera.
	shareQueryTimeout   = 5 * time.Second
	shareCommandTimeout = 8 * time.Second
	shareBodyLimit      = 4 << 10
)

// shareReleaseWait bounds how long a direct start on a one-session camera waits
// for the integration to release its own session. It polls /status every 2 s
// and checks again 0.2, 0.5, 1 and 3 s after asking for a stream. A variable
// for tests.
var shareReleaseWait = 6 * time.Second

var shareCameraID = regexp.MustCompile(`^[A-Za-z0-9_-]{1,64}$`)

// shareClient is what the hub needs from a camera session.
type shareClient interface {
	SetDPS(ctx context.Context, dps map[string]any) error
	QueryDPS(ctx context.Context) (map[string]any, error)
}

// ShareEvent is one event on a subscriber's stream.
type ShareEvent struct {
	Type string         `json:"type"` // hello, dps, ending
	Seq  uint64         `json:"seq"`
	At   time.Time      `json:"at"`
	DPS  map[string]any `json:"dps,omitempty"`
	// Exclusive on "ending": the session is already closed (a one-session
	// camera), so the integration reconnects its own without waiting.
	Exclusive bool `json:"exclusive,omitempty"`
}

type shareSub struct {
	camera string
	ch     chan ShareEvent
	closed bool
}

// ShareCounters is what /status shows for a camera's local sessions.
type ShareCounters struct {
	Pushes        uint64
	Sessions      uint64
	SessionErrors uint64
	Subscribed    bool
	// WantsSession: a direct start on a one-session camera waits for the
	// integration to release its own session.
	WantsSession bool
}

type shareCam struct {
	client   shareClient
	subs     map[*shareSub]struct{}
	seq      uint64
	counters ShareCounters
	// linger is the close of an ended stream's session that waits for its
	// subscribers to leave; lingerGone is closed once it ran.
	linger     func()
	lingerGone chan struct{}
	lingerFree chan struct{}
	// exclusive: the camera serves one session; its end closes at once.
	exclusive bool
	// released is closed by the integration's release while a start waits.
	released chan struct{}
	// starting counts direct starts that have the integration's released
	// session and are still connecting: wants_session stays up until they
	// end, or the integration took its session back from a slow start.
	starting int
}

// ShareHub routes the data points of the bridge's camera sessions. The zero
// value is ready to use.
type ShareHub struct {
	mu     sync.Mutex
	cams   map[string]*shareCam
	linger time.Duration // shareLinger when zero
}

func (h *ShareHub) cam(id string) *shareCam {
	if h.cams == nil {
		h.cams = map[string]*shareCam{}
	}
	c, ok := h.cams[id]
	if !ok {
		c = &shareCam{subs: map[*shareSub]struct{}{}}
		h.cams[id] = c
	}
	return c
}

// attach makes client the camera's session for subscribers and commands.
func (h *ShareHub) attach(id string, client shareClient) {
	h.mu.Lock()
	defer h.mu.Unlock()
	c := h.cam(id)
	c.client = client
	c.counters.Sessions++
}

func (h *ShareHub) noteSessionError(id string) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.cam(id).counters.SessionErrors++
}

// publish passes pushed data points to every subscriber of the camera. A
// subscriber whose buffer is full is cut off rather than slowing the session.
func (h *ShareHub) publish(id string, dps map[string]any, at time.Time) {
	h.mu.Lock()
	defer h.mu.Unlock()
	c := h.cam(id)
	c.seq++
	c.counters.Pushes++
	ev := ShareEvent{Type: "dps", Seq: c.seq, At: at, DPS: dps}
	for sub := range c.subs {
		select {
		case sub.ch <- ev:
		default:
			h.dropLocked(c, sub)
		}
	}
}

func (h *ShareHub) dropLocked(c *shareCam, sub *shareSub) {
	if sub.closed {
		return
	}
	sub.closed = true
	close(sub.ch)
	delete(c.subs, sub)
	if len(c.subs) == 0 && c.lingerFree != nil {
		close(c.lingerFree)
		c.lingerFree = nil
	}
}

var errNoLocalSession = errors.New("no local session for this camera")

// subscribe registers a subscriber for a camera with a running session.
func (h *ShareHub) subscribe(id string) (*shareSub, shareClient, error) {
	h.mu.Lock()
	defer h.mu.Unlock()
	c := h.cam(id)
	if c.client == nil || c.linger != nil {
		return nil, nil, errNoLocalSession
	}
	sub := &shareSub{camera: id, ch: make(chan ShareEvent, shareBuffer)}
	c.subs[sub] = struct{}{}
	return sub, c.client, nil
}

func (h *ShareHub) unsubscribe(sub *shareSub) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.dropLocked(h.cam(sub.camera), sub)
}

// nextSeq is the sequence number a hello carries: events after it are new.
func (h *ShareHub) nextSeq(id string) uint64 {
	h.mu.Lock()
	defer h.mu.Unlock()
	return h.cam(id).seq
}

// setExclusive marks whether the camera serves one session at a time.
func (h *ShareHub) setExclusive(id string, exclusive bool) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.cam(id).exclusive = exclusive
}

// requestSession asks the integration to release its own session to the
// camera (wants_session in /status) and waits for its confirmation, at most
// wait, or until ctx ends. It reports whether the session was released.
func (h *ShareHub) requestSession(ctx context.Context, id string, wait time.Duration) bool {
	h.mu.Lock()
	c := h.cam(id)
	released := make(chan struct{})
	c.released = released
	h.mu.Unlock()
	timer := time.NewTimer(wait)
	defer timer.Stop()
	got := false
	select {
	case <-released:
		got = true
	case <-timer.C:
	case <-ctx.Done():
	}
	h.mu.Lock()
	if c.released == released {
		c.released = nil
	}
	h.mu.Unlock()
	return got
}

// holdForStart keeps wants_session up while a direct start that got the
// integration's session connects; the returned func ends that, once.
func (h *ShareHub) holdForStart(id string) func() {
	h.mu.Lock()
	h.cam(id).starting++
	h.mu.Unlock()
	var once sync.Once
	return func() {
		once.Do(func() {
			h.mu.Lock()
			h.cam(id).starting--
			h.mu.Unlock()
		})
	}
}

// noteReleased is the integration's confirmation that its own session to the
// camera is closed. It reports whether a start was waiting for it.
func (h *ShareHub) noteReleased(id string) bool {
	h.mu.Lock()
	defer h.mu.Unlock()
	c, ok := h.cams[id]
	if !ok || c.released == nil {
		return false
	}
	close(c.released)
	c.released = nil
	return true
}

// release ends a camera session. Without subscribers it closes at once; with
// subscribers it announces the end and closes once they have left, at most
// after the linger time, so the integration can reopen its own session first.
// It never blocks the caller.
func (h *ShareHub) release(id string, client shareClient, closeFn func()) {
	h.mu.Lock()
	c := h.cam(id)
	if c.client != client {
		h.mu.Unlock()
		closeFn()
		return
	}
	if len(c.subs) == 0 {
		c.client = nil
		h.mu.Unlock()
		closeFn()
		return
	}
	c.seq++
	ev := ShareEvent{Type: "ending", Seq: c.seq, At: time.Now(), Exclusive: c.exclusive}
	for sub := range c.subs {
		select {
		case sub.ch <- ev:
		default:
			h.dropLocked(c, sub)
		}
	}
	if c.exclusive {
		// One session at a time: the integration can only reconnect once this
		// one is gone, so it closes now instead of waiting for the subscriber.
		c.client = nil
		h.mu.Unlock()
		closeFn()
		return
	}
	var once sync.Once
	gone := make(chan struct{})
	run := func() {
		once.Do(func() {
			closeFn()
			h.mu.Lock()
			if c.client == client {
				c.client = nil
			}
			c.linger, c.lingerFree = nil, nil
			h.mu.Unlock()
			close(gone)
		})
	}
	free := make(chan struct{})
	if len(c.subs) == 0 {
		close(free)
	} else {
		c.lingerFree = free
	}
	c.linger, c.lingerGone = run, gone
	limit := h.linger
	if limit <= 0 {
		limit = shareLinger
	}
	h.mu.Unlock()

	go func() {
		select {
		case <-free:
		case <-time.After(limit):
			core.Logger.Info().Msgf("Share: a subscriber of %s did not leave within %s; closing its session", id, limit)
		}
		run()
	}()
}

// flush closes a lingering session of the camera now, before a new local
// start opens another one.
func (h *ShareHub) flush(id string) {
	h.mu.Lock()
	var run func()
	if c, ok := h.cams[id]; ok {
		run = c.linger
	}
	h.mu.Unlock()
	if run != nil {
		run()
	}
}

func (h *ShareHub) counters(id string) ShareCounters {
	h.mu.Lock()
	defer h.mu.Unlock()
	c, ok := h.cams[id]
	if !ok {
		return ShareCounters{}
	}
	out := c.counters
	out.Subscribed = len(c.subs) > 0
	out.WantsSession = c.released != nil || c.starting > 0
	return out
}

func (h *ShareHub) command(ctx context.Context, id string, dps map[string]any) error {
	h.mu.Lock()
	var client shareClient
	if c, ok := h.cams[id]; ok && c.linger == nil {
		client = c.client
	}
	h.mu.Unlock()
	if client == nil {
		return errNoLocalSession
	}
	return client.SetDPS(ctx, dps)
}

// shareAuthorized checks the bearer token in constant time. The token never
// reaches a log line.
func (s *RTSPServer) shareAuthorized(r *http.Request) bool {
	if s.APIToken == "" {
		return false
	}
	got, ok := strings.CutPrefix(r.Header.Get("Authorization"), "Bearer ")
	if !ok {
		return false
	}
	return subtle.ConstantTimeCompare([]byte(got), []byte(s.APIToken)) == 1
}

// shareHandler serves /cameras/<id>/events and /cameras/<id>/dps.
func (s *RTSPServer) shareHandler(w http.ResponseWriter, r *http.Request) {
	if s.APIToken == "" {
		http.NotFound(w, r)
		return
	}
	if !s.shareAuthorized(r) {
		w.WriteHeader(http.StatusUnauthorized)
		return
	}
	rest := strings.TrimPrefix(r.URL.Path, "/cameras/")
	id, what, ok := strings.Cut(rest, "/")
	if !ok || !shareCameraID.MatchString(id) {
		http.NotFound(w, r)
		return
	}
	switch {
	case what == "events" && r.Method == http.MethodGet:
		s.shareEvents(w, r, id)
	case what == "dps" && r.Method == http.MethodPost:
		s.shareCommand(w, r, id)
	case what == "release" && r.Method == http.MethodPost:
		if s.share.noteReleased(id) {
			w.WriteHeader(http.StatusNoContent)
		} else {
			// Nothing waits for it (any more): the integration takes its own
			// session back.
			w.WriteHeader(http.StatusConflict)
		}
	default:
		w.WriteHeader(http.StatusMethodNotAllowed)
	}
}

func writeSSE(w io.Writer, ev ShareEvent) error {
	body, err := json.Marshal(ev)
	if err != nil {
		return err
	}
	_, err = io.WriteString(w, "event: "+ev.Type+"\ndata: "+string(body)+"\n\n")
	return err
}

func (s *RTSPServer) shareEvents(w http.ResponseWriter, r *http.Request, id string) {
	// Subscribed before the query, so a push that arrives meanwhile waits in
	// the buffer instead of being missed.
	sub, client, err := s.share.subscribe(id)
	if err != nil {
		http.Error(w, err.Error(), http.StatusConflict)
		return
	}
	defer s.share.unsubscribe(sub)
	seq := s.share.nextSeq(id)
	qctx, cancel := context.WithTimeout(r.Context(), shareQueryTimeout)
	dps, err := client.QueryDPS(qctx)
	cancel()
	if err != nil {
		// No answer, so no proof the session carries data points: the
		// integration keeps its own session.
		http.Error(w, "the local session did not answer a query", http.StatusBadGateway)
		return
	}
	// An answer without points proves the session as well: it decrypted under
	// the local key. tuya-lan hands one that is no JSON over as an empty set,
	// and an SCD973-family unit on 3.3 answers command 10 with "json obj data
	// unvalid" nearly every time (03-11-2026: 8 of 8 queries during a stream;
	// one answer with a single point right after its session started). Asking
	// for points refused the handover by chance, in 5 of 19 streams on
	// 02-11-2026. The hello then carries no points; the pushes still follow.
	rc := http.NewResponseController(w)
	_ = rc.SetWriteDeadline(time.Time{})
	w.Header().Set("Content-Type", "text/event-stream")
	w.Header().Set("Cache-Control", "no-store")
	w.WriteHeader(http.StatusOK)
	if writeSSE(w, ShareEvent{Type: "hello", Seq: seq, At: time.Now(), DPS: dps}) != nil || rc.Flush() != nil {
		return
	}
	ping := time.NewTicker(sharePing)
	defer ping.Stop()
	for {
		select {
		case <-r.Context().Done():
			return
		case ev, ok := <-sub.ch:
			if !ok {
				return
			}
			if writeSSE(w, ev) != nil || rc.Flush() != nil {
				return
			}
		case <-ping.C:
			if _, err := io.WriteString(w, ": ping\n\n"); err != nil || rc.Flush() != nil {
				return
			}
		}
	}
}

func (s *RTSPServer) shareCommand(w http.ResponseWriter, r *http.Request, id string) {
	var req struct {
		DPS map[string]any `json:"dps"`
	}
	if err := json.NewDecoder(io.LimitReader(r.Body, shareBodyLimit)).Decode(&req); err != nil {
		http.Error(w, "bad request", http.StatusBadRequest)
		return
	}
	if !tuyalan.ValidDPS(req.DPS) {
		http.Error(w, "invalid data points", http.StatusBadRequest)
		return
	}
	// The server's WriteTimeout (5 s) runs from the request: a camera that
	// answers after that applied the command, but the integration saw a broken
	// connection. Give the answer the command's own time and a little more.
	_ = http.NewResponseController(w).SetWriteDeadline(time.Now().Add(shareCommandTimeout + 2*time.Second))
	ctx, cancel := context.WithTimeout(r.Context(), shareCommandTimeout)
	defer cancel()
	started := time.Now()
	err := s.share.command(ctx, id, req.DPS)
	w.Header().Set("Content-Type", "application/json")
	switch {
	case errors.Is(err, errNoLocalSession):
		w.WriteHeader(http.StatusConflict)
	case err != nil:
		w.WriteHeader(http.StatusBadGateway)
	}
	msg := ""
	if err != nil {
		msg = err.Error()
	}
	_ = json.NewEncoder(w).Encode(map[string]any{"ok": err == nil, "error": msg, "ms": time.Since(started).Milliseconds()})
}
