package rtsp

import (
	"bufio"
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"net/http"
	"net/http/httptest"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"
)

// fakeShareClient stands in for a camera session; open counts sessions that
// are open across all fakes of a test.
type fakeShareClient struct {
	open     *atomic.Int32
	closed   atomic.Bool
	queryDPS map[string]any
	queryErr error
	mu       sync.Mutex
	sets     []map[string]any
}

func newFakeShareClient(open *atomic.Int32) *fakeShareClient {
	open.Add(1)
	return &fakeShareClient{open: open, queryDPS: map[string]any{"212": "x", "246": "playing"}}
}

func (f *fakeShareClient) Close() {
	if f.closed.CompareAndSwap(false, true) {
		f.open.Add(-1)
	}
}

func (f *fakeShareClient) SetDPS(_ context.Context, dps map[string]any) error {
	f.mu.Lock()
	defer f.mu.Unlock()
	f.sets = append(f.sets, dps)
	return nil
}

func (f *fakeShareClient) QueryDPS(context.Context) (map[string]any, error) {
	return f.queryDPS, f.queryErr
}

func waitFor(t *testing.T, what string, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(2 * time.Second)
	for !cond() {
		if time.Now().After(deadline) {
			t.Fatalf("timed out waiting for %s", what)
		}
		time.Sleep(5 * time.Millisecond)
	}
}

func TestSharePushesReachEverySubscriberInOrder(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	h.attach("cam", newFakeShareClient(&open))
	a, _, err := h.subscribe("cam")
	if err != nil {
		t.Fatal(err)
	}
	b, _, _ := h.subscribe("cam")
	h.publish("cam", map[string]any{"212": "one"}, time.Now())
	h.publish("cam", map[string]any{"212": "two"}, time.Now())
	for _, sub := range []*shareSub{a, b} {
		first, second := <-sub.ch, <-sub.ch
		if first.Seq+1 != second.Seq || first.DPS["212"] != "one" || second.DPS["212"] != "two" {
			t.Fatalf("got %+v then %+v", first, second)
		}
	}
	if c := h.counters("cam"); c.Pushes != 2 || c.Sessions != 1 || !c.Subscribed {
		t.Fatalf("counters %+v", c)
	}
}

// A subscriber that falls behind is cut off, so the integration takes its own
// session back instead of silently missing pushes.
func TestShareCutsOffASlowSubscriber(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	h.attach("cam", newFakeShareClient(&open))
	sub, _, _ := h.subscribe("cam")
	for i := 0; i <= shareBuffer; i++ {
		h.publish("cam", map[string]any{"1": i}, time.Now())
	}
	n := 0
	for range sub.ch {
		n++
	}
	if n != shareBuffer {
		t.Fatalf("read %d events before the cut, want %d", n, shareBuffer)
	}
	if h.counters("cam").Subscribed {
		t.Fatal("the slow subscriber is still registered")
	}
}

func TestShareReleaseWithoutSubscribersClosesAtOnce(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	c := newFakeShareClient(&open)
	h.attach("cam", c)
	h.release("cam", c, c.Close)
	if !c.closed.Load() {
		t.Fatal("session not closed")
	}
}

// With a subscriber, the end is announced and the session stays until the
// subscriber leaves: the integration reopens its own session in between.
func TestShareReleaseWaitsForTheSubscriber(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	c := newFakeShareClient(&open)
	h.attach("cam", c)
	sub, _, _ := h.subscribe("cam")
	h.release("cam", c, c.Close)
	if ev := <-sub.ch; ev.Type != "ending" {
		t.Fatalf("got %q, want ending", ev.Type)
	}
	time.Sleep(50 * time.Millisecond)
	if c.closed.Load() {
		t.Fatal("closed while the subscriber was still there")
	}
	if _, _, err := h.subscribe("cam"); err == nil {
		t.Fatal("a new subscriber was accepted on an ending session")
	}
	if err := h.command(context.Background(), "cam", map[string]any{"201": "stop"}); err == nil {
		t.Fatal("a command went to an ending session")
	}
	h.unsubscribe(sub)
	waitFor(t, "the close", c.closed.Load)
}

func TestShareReleaseGivesUpOnAStuckSubscriber(t *testing.T) {
	h := ShareHub{linger: 50 * time.Millisecond}
	var open atomic.Int32
	c := newFakeShareClient(&open)
	h.attach("cam", c)
	_, _, _ = h.subscribe("cam")
	h.release("cam", c, c.Close)
	waitFor(t, "the close after the linger", c.closed.Load)
}

// A new local start closes a lingering session before it opens its own, so
// the bridge never holds two sessions to one camera.
func TestShareNeverTwoBridgeSessions(t *testing.T) {
	var h ShareHub
	var open atomic.Int32
	old := newFakeShareClient(&open)
	h.attach("cam", old)
	_, _, _ = h.subscribe("cam")
	h.release("cam", old, old.Close)
	if open.Load() != 1 {
		t.Fatalf("%d sessions open while lingering", open.Load())
	}
	h.flush("cam") // what tryLAN does before Start
	if open.Load() != 0 {
		t.Fatalf("%d sessions open after flush, want 0 before the new start", open.Load())
	}
	next := newFakeShareClient(&open)
	h.attach("cam", next)
	if open.Load() != 1 {
		t.Fatalf("%d sessions open, want 1", open.Load())
	}
}

func shareServer(token string) *RTSPServer {
	s := NewRTSPServer(0, nil)
	s.APIToken = token
	return s
}

func TestShareEndpointsNeedTheToken(t *testing.T) {
	const token = "secret-token-for-the-test-1234567890"
	srv := httptest.NewServer(shareServer(token).statusHandler())
	defer srv.Close()
	for _, auth := range []string{"", "Bearer wrong", "Basic " + token, token} {
		req, _ := http.NewRequest(http.MethodGet, srv.URL+"/cameras/cam/events", nil)
		if auth != "" {
			req.Header.Set("Authorization", auth)
		}
		resp, err := http.DefaultClient.Do(req)
		if err != nil {
			t.Fatal(err)
		}
		resp.Body.Close()
		if resp.StatusCode != http.StatusUnauthorized {
			t.Fatalf("%q: status %d, want 401", auth, resp.StatusCode)
		}
	}
	// Without a token in the config the endpoints do not exist at all.
	off := httptest.NewServer(shareServer("").statusHandler())
	defer off.Close()
	resp, err := http.Get(off.URL + "/cameras/cam/events")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusNotFound {
		t.Fatalf("status %d without a configured token, want 404", resp.StatusCode)
	}
	// Nor does /status show anything of it.
	resp, _ = http.Get(srv.URL + "/status")
	var doc bytes.Buffer
	_, _ = doc.ReadFrom(resp.Body)
	resp.Body.Close()
	if strings.Contains(doc.String(), token) {
		t.Fatal("the token appears in /status")
	}
}

func authed(t *testing.T, method, url, token, body string) *http.Response {
	t.Helper()
	req, _ := http.NewRequest(method, url, strings.NewReader(body))
	req.Header.Set("Authorization", "Bearer "+token)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		t.Fatal(err)
	}
	return resp
}

func readEvent(t *testing.T, r *bufio.Reader) ShareEvent {
	t.Helper()
	for {
		line, err := r.ReadString('\n')
		if err != nil {
			t.Fatal(err)
		}
		if data, ok := strings.CutPrefix(strings.TrimSpace(line), "data: "); ok {
			var ev ShareEvent
			if err := json.Unmarshal([]byte(data), &ev); err != nil {
				t.Fatal(err)
			}
			return ev
		}
	}
}

// The hello carries a fresh query, which proves the session delivers data
// points; pushes follow with higher sequence numbers.
func TestShareEventsHelloThenPushes(t *testing.T) {
	const token = "tok"
	s := shareServer(token)
	var open atomic.Int32
	s.share.attach("cam", newFakeShareClient(&open))
	srv := httptest.NewServer(s.statusHandler())
	defer srv.Close()

	resp := authed(t, http.MethodGet, srv.URL+"/cameras/cam/events", token, "")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d", resp.StatusCode)
	}
	r := bufio.NewReader(resp.Body)
	hello := readEvent(t, r)
	if hello.Type != "hello" || hello.DPS["246"] != "playing" {
		t.Fatalf("hello %+v", hello)
	}
	s.share.publish("cam", map[string]any{"212": "alarm"}, time.Now())
	ev := readEvent(t, r)
	if ev.Type != "dps" || ev.DPS["212"] != "alarm" || ev.Seq <= hello.Seq {
		t.Fatalf("push %+v after hello seq %d", ev, hello.Seq)
	}
}

// Without proof that the session carries data points the subscription is
// refused, and the integration keeps its own session.
func TestShareEventsRefusedWhenTheQueryFails(t *testing.T) {
	const token = "tok"
	s := shareServer(token)
	var open atomic.Int32
	c := newFakeShareClient(&open)
	c.queryErr = errors.New("no answer")
	s.share.attach("cam", c)
	srv := httptest.NewServer(s.statusHandler())
	defer srv.Close()
	resp := authed(t, http.MethodGet, srv.URL+"/cameras/cam/events", token, "")
	resp.Body.Close()
	if resp.StatusCode != http.StatusBadGateway {
		t.Fatalf("status %d, want 502", resp.StatusCode)
	}
	if s.share.counters("cam").Subscribed {
		t.Fatal("the refused subscriber stayed registered")
	}
	resp = authed(t, http.MethodGet, srv.URL+"/cameras/other/events", token, "")
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("status %d for a camera without a session, want 409", resp.StatusCode)
	}
}

// An answer without readable points (3.3's "json obj data unvalid") still
// proves the session: the subscription is taken, with a hello without points.
func TestShareEventsHelloWithoutPoints(t *testing.T) {
	const token = "tok"
	s := shareServer(token)
	var open atomic.Int32
	c := newFakeShareClient(&open)
	c.queryDPS = map[string]any{}
	s.share.attach("cam", c)
	srv := httptest.NewServer(s.statusHandler())
	defer srv.Close()
	resp := authed(t, http.MethodGet, srv.URL+"/cameras/cam/events", token, "")
	defer resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("status %d, want 200", resp.StatusCode)
	}
	hello := readEvent(t, bufio.NewReader(resp.Body))
	if hello.Type != "hello" || len(hello.DPS) != 0 {
		t.Fatalf("hello %+v", hello)
	}
}

func TestShareCommand(t *testing.T) {
	const token = "tok"
	s := shareServer(token)
	var open atomic.Int32
	c := newFakeShareClient(&open)
	s.share.attach("cam", c)
	srv := httptest.NewServer(s.statusHandler())
	defer srv.Close()

	resp := authed(t, http.MethodPost, srv.URL+"/cameras/cam/dps", token, `{"dps":{"201":"stop"}}`)
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK || len(c.sets) != 1 || c.sets[0]["201"] != "stop" {
		t.Fatalf("status %d, sets %v", resp.StatusCode, c.sets)
	}
	for _, body := range []string{`{"dps":{}}`, `{"dps":{"abc":1}}`, `not json`} {
		resp = authed(t, http.MethodPost, srv.URL+"/cameras/cam/dps", token, body)
		resp.Body.Close()
		if resp.StatusCode != http.StatusBadRequest {
			t.Fatalf("%s: status %d, want 400", body, resp.StatusCode)
		}
	}
	resp = authed(t, http.MethodPost, srv.URL+"/cameras/other/dps", token, `{"dps":{"201":"stop"}}`)
	resp.Body.Close()
	if resp.StatusCode != http.StatusConflict {
		t.Fatalf("status %d without a session, want 409", resp.StatusCode)
	}
}
