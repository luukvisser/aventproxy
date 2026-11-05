package rtsp

import (
	"bufio"
	"bytes"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/core"
	"avent-webrtc-bridge/pkg/storage"

	"github.com/rs/zerolog"
)

const testRTSPPassword = "a-password-for-the-test-0123456789"

// digestHeader answers a challenge the way ffmpeg does (no qop) or with
// qop=auth when cnonce is set.
func digestHeader(user, password, method, uri, nonce, cnonce string) string {
	ha1 := md5hex(user + ":" + digestRealm + ":" + password)
	ha2 := md5hex(method + ":" + uri)
	if cnonce == "" {
		resp := md5hex(ha1 + ":" + nonce + ":" + ha2)
		return fmt.Sprintf(`Digest username="%s", realm="%s", nonce="%s", uri="%s", response="%s"`, user, digestRealm, nonce, uri, resp)
	}
	resp := md5hex(ha1 + ":" + nonce + ":00000001:" + cnonce + ":auth:" + ha2)
	return fmt.Sprintf(`Digest username="%s", realm="%s", nonce="%s", uri="%s", qop=auth, nc=00000001, cnonce="%s", response="%s", algorithm=MD5`,
		user, digestRealm, nonce, uri, cnonce, resp)
}

func TestDigestAcceptsTheRightAnswer(t *testing.T) {
	a := newDigestAuth(RTSPUser, testRTSPPassword)
	uri := "rtsp://bridge:38554/Cam"
	for _, cnonce := range []string{"", "0a4f113b"} {
		h := digestHeader(RTSPUser, testRTSPPassword, "DESCRIBE", uri, a.nonce(), cnonce)
		if ok, stale := a.check("DESCRIBE", uri, h); !ok || stale {
			t.Fatalf("cnonce %q: ok=%v stale=%v", cnonce, ok, stale)
		}
	}
	// A path-only uri for an absolute request URI, as some clients send.
	h := digestHeader(RTSPUser, testRTSPPassword, "OPTIONS", "/Cam", a.nonce(), "")
	if ok, _ := a.check("OPTIONS", uri, h); !ok {
		t.Fatal("a path-only uri was refused")
	}
}

func TestDigestRefusesEverythingElse(t *testing.T) {
	a := newDigestAuth(RTSPUser, testRTSPPassword)
	other := newDigestAuth(RTSPUser, testRTSPPassword)
	uri := "rtsp://bridge:38554/Cam"
	n := a.nonce()
	for name, h := range map[string]string{
		"none":           "",
		"basic":          "Basic aG9tZWFzc2lzdGFudDphLXBhc3N3b3Jk",
		"wrong password": digestHeader(RTSPUser, "guess", "DESCRIBE", uri, n, ""),
		"wrong user":     digestHeader("admin", testRTSPPassword, "DESCRIBE", uri, n, ""),
		"other method":   digestHeader(RTSPUser, testRTSPPassword, "OPTIONS", uri, n, ""),
		"other uri":      digestHeader(RTSPUser, testRTSPPassword, "DESCRIBE", "rtsp://bridge:38554/Other", n, ""),
		"made-up nonce":  digestHeader(RTSPUser, testRTSPPassword, "DESCRIBE", uri, "65000000.00", ""),
		"foreign nonce":  digestHeader(RTSPUser, testRTSPPassword, "DESCRIBE", uri, other.nonce(), ""),
	} {
		if ok, stale := a.check("DESCRIBE", uri, h); ok || stale {
			t.Errorf("%s: ok=%v stale=%v", name, ok, stale)
		}
	}
}

func TestDigestExpiredNonceIsStale(t *testing.T) {
	a := newDigestAuth(RTSPUser, testRTSPPassword)
	start := time.Now()
	a.now = func() time.Time { return start }
	n := a.nonce()
	a.now = func() time.Time { return start.Add(nonceLifetime + time.Second) }
	uri := "rtsp://bridge:38554/Cam"
	ok, stale := a.check("PLAY", uri, digestHeader(RTSPUser, testRTSPPassword, "PLAY", uri, n, ""))
	if ok || !stale {
		t.Fatalf("expired nonce: ok=%v stale=%v, want stale", ok, stale)
	}
	if !strings.Contains(a.challenge(true), "stale=true") {
		t.Fatal("the challenge does not say stale")
	}
}

// rtspExchange writes one request on c and reads the response head.
func rtspExchange(t *testing.T, c net.Conn, r *bufio.Reader, req string) (int, map[string]string) {
	t.Helper()
	_ = c.SetDeadline(time.Now().Add(3 * time.Second))
	// An empty write on a net.Pipe blocks; "" only reads the next response.
	if req != "" {
		if _, err := io.WriteString(c, req); err != nil {
			t.Fatalf("write: %v", err)
		}
	}
	line, err := r.ReadString('\n')
	if err != nil {
		t.Fatalf("read status: %v", err)
	}
	var code int
	fmt.Sscanf(line, "RTSP/1.0 %d", &code)
	headers := map[string]string{}
	for {
		l, err := r.ReadString('\n')
		if err != nil {
			t.Fatalf("read header: %v", err)
		}
		l = strings.TrimRight(l, "\r\n")
		if l == "" {
			return code, headers
		}
		k, v, _ := strings.Cut(l, ":")
		headers[k] = strings.TrimSpace(v)
	}
}

func authServer() *RTSPServer {
	s := NewRTSPServer(0, nil)
	s.RTSPPassword = testRTSPPassword
	s.auth = newDigestAuth(RTSPUser, testRTSPPassword)
	return s
}

// Without the password a connection gets challenges, never a camera lookup
// or a stream (the server has no storage: a lookup would panic), and is
// closed after maxUnauthorized requests.
func TestUnauthenticatedConnectionStartsNothing(t *testing.T) {
	s := authServer()
	server, client := net.Pipe()
	done := make(chan struct{})
	go func() { s.handleConnection(server); close(done) }()
	r := bufio.NewReader(client)
	for i := 1; i <= maxUnauthorized; i++ {
		code, h := rtspExchange(t, client, r, fmt.Sprintf("OPTIONS rtsp://bridge:38554/Cam RTSP/1.0\r\nCSeq: %d\r\n\r\n", i))
		if code != 401 || !strings.HasPrefix(h["WWW-Authenticate"], "Digest ") || h["CSeq"] != fmt.Sprint(i) {
			t.Fatalf("request %d: %d %v", i, code, h)
		}
	}
	select {
	case <-done:
	case <-time.After(3 * time.Second):
		t.Fatal("the connection stayed open after the refused requests")
	}
	if len(s.streams) != 0 || len(s.clients) != 0 {
		t.Fatalf("an unauthenticated connection made %d streams, %d clients", len(s.streams), len(s.clients))
	}
}

// With the right answer the request goes on to the path check: an empty path
// is a 400, not a 401.
func TestAuthenticatedRequestIsServed(t *testing.T) {
	s := authServer()
	server, client := net.Pipe()
	go s.handleConnection(server)
	defer client.Close()
	r := bufio.NewReader(client)
	uri := "rtsp://bridge:38554/"
	code, h := rtspExchange(t, client, r, "OPTIONS "+uri+" RTSP/1.0\r\nCSeq: 1\r\n\r\n")
	if code != 401 {
		t.Fatalf("first request %d", code)
	}
	nonce := parseDigestParams(strings.TrimPrefix(h["WWW-Authenticate"], "Digest "))["nonce"]
	auth := digestHeader(RTSPUser, testRTSPPassword, "OPTIONS", uri, nonce, "")
	code, _ = rtspExchange(t, client, r, "OPTIONS "+uri+" RTSP/1.0\r\nCSeq: 2\r\nAuthorization: "+auth+"\r\n\r\n")
	if code != 400 {
		t.Fatalf("authorized request answered %d, want 400 for the empty path", code)
	}
}

// Without a password the server answers as before, unauthenticated.
func TestNoPasswordNoAuthentication(t *testing.T) {
	s := NewRTSPServer(0, nil)
	if !s.authorize(nil, &RTSPRequest{Method: "OPTIONS", URL: "rtsp://x/Cam"}) {
		t.Fatal("a server without a password refused a request")
	}
}

func TestConnectionCap(t *testing.T) {
	s := authServer()
	s.MaxConnections = 1
	s.connections.Store(1)
	server, client := net.Pipe()
	done := make(chan struct{})
	go func() { s.handleConnection(server); close(done) }()
	select {
	case <-done:
	case <-time.After(2 * time.Second):
		t.Fatal("a connection over the cap was served")
	}
	_ = client.SetReadDeadline(time.Now().Add(time.Second))
	if _, err := client.Read(make([]byte, 1)); err == nil {
		t.Fatal("a connection over the cap got an answer")
	}
	if got := s.connections.Load(); got != 1 {
		t.Fatalf("connection count %d after the refusal, want 1", got)
	}
}

func TestRequestLimits(t *testing.T) {
	s := NewRTSPServer(0, nil)
	var many strings.Builder
	many.WriteString("OPTIONS rtsp://x/Cam RTSP/1.0\r\n")
	for i := 0; i <= maxRequestHeaders; i++ {
		fmt.Fprintf(&many, "X-H%d: v\r\n", i)
	}
	many.WriteString("\r\n")
	long := "OPTIONS rtsp://x/Cam RTSP/1.0\r\nX-Long: " + strings.Repeat("a", 5000) + "\r\n\r\n"
	longLine := "OPTIONS rtsp://x/" + strings.Repeat("a", 5000) + " RTSP/1.0\r\n\r\n"
	for name, req := range map[string]string{"headers": many.String(), "header line": long, "request line": longLine} {
		if _, err := s.parseRTSPRequestFromReader(bufio.NewReader(strings.NewReader(req))); err == nil {
			t.Errorf("%s: an oversized request was accepted", name)
		}
	}
	ok := "OPTIONS rtsp://x/Cam RTSP/1.0\r\nCSeq: 3\r\n\r\n"
	if r, err := s.parseRTSPRequestFromReader(bufio.NewReader(strings.NewReader(ok))); err != nil || r.CSeq != 3 {
		t.Fatalf("a normal request: %v %+v", err, r)
	}
}

// The trace line of a request never carries its Authorization header: it
// names the method, the URL and the CSeq only.
func TestRequestTraceRedactsCredentials(t *testing.T) {
	var buf bytes.Buffer
	old := core.Logger
	core.Logger = zerolog.New(&buf).Level(zerolog.TraceLevel)
	defer func() { core.Logger = old }()
	s := NewRTSPServer(0, nil)
	req := "OPTIONS rtsp://x/Cam RTSP/1.0\r\nCSeq: 1\r\nAuthorization: Digest response=\"" + testRTSPPassword + "\"\r\n\r\n"
	if _, err := s.parseRTSPRequestFromReader(bufio.NewReader(strings.NewReader(req))); err != nil {
		t.Fatal(err)
	}
	if strings.Contains(buf.String(), testRTSPPassword) || !strings.Contains(buf.String(), "OPTIONS") {
		t.Fatalf("trace line: %s", buf.String())
	}
}

func TestBackchannelRefusedWithoutTalkback(t *testing.T) {
	s := NewRTSPServer(0, nil)
	server, client := net.Pipe()
	defer client.Close()
	c := &RTSPClient{conn: server, session: "s1"}
	go s.handleSetup(c, &RTSPRequest{Method: "SETUP", URL: "rtsp://x/Cam/backchannel", CSeq: 4,
		Headers: map[string]string{"Transport": "RTP/AVP;unicast;client_port=5000-5001"}})
	code, _ := rtspExchange(t, client, bufio.NewReader(client), "")
	if code != 403 {
		t.Fatalf("backchannel SETUP without talkback answered %d, want 403", code)
	}
}

// With talkback on, a local stream still has no return channel to set up.
func TestBackchannelRefusedOnALocalStream(t *testing.T) {
	s := NewRTSPServer(0, nil)
	s.Talkback = true
	s.TalkbackMode = TalkbackOnDemand
	stream := NewCameraStreamForMode(&storage.CameraInfo{DeviceID: "d", DeviceName: "Cam"}, "hd", ModeLocal, nil, nil, s)
	server, client := net.Pipe()
	defer client.Close()
	c := &RTSPClient{conn: server, session: "s1", stream: stream}
	go s.handleSetup(c, &RTSPRequest{Method: "SETUP", URL: "rtsp://x/Cam/local/backchannel", CSeq: 4,
		Headers: map[string]string{"Transport": "RTP/AVP;unicast;client_port=5000-5001"}})
	code, _ := rtspExchange(t, client, bufio.NewReader(client), "")
	if code != 403 {
		t.Fatalf("backchannel SETUP on a local stream answered %d, want 403", code)
	}
}

func TestBackchannelOnlyFromTheClient(t *testing.T) {
	peer := net.ParseIP("192.0.2.3")
	if !backchannelSender(peer, &net.UDPAddr{IP: net.ParseIP("192.0.2.3"), Port: 5000}) {
		t.Fatal("the client's own packet was dropped")
	}
	if backchannelSender(peer, &net.UDPAddr{IP: net.ParseIP("192.0.2.1"), Port: 5000}) {
		t.Fatal("a packet from another address was accepted")
	}
	if !backchannelSender(nil, &net.UDPAddr{IP: net.ParseIP("192.0.2.1")}) {
		t.Fatal("without a peer every sender is accepted, as before")
	}
}

func TestStatusNeedsTheTokenWhenSet(t *testing.T) {
	const token = "secret-token-for-the-test-1234567890"
	srv := httptest.NewServer(shareServer(token).statusHandler())
	defer srv.Close()
	resp, err := http.Get(srv.URL + "/status")
	if err != nil {
		t.Fatal(err)
	}
	resp.Body.Close()
	if resp.StatusCode != http.StatusUnauthorized {
		t.Fatalf("/status without the token: %d, want 401", resp.StatusCode)
	}
	resp = authed(t, http.MethodGet, srv.URL+"/status", token, "")
	resp.Body.Close()
	if resp.StatusCode != http.StatusOK {
		t.Fatalf("/status with the token: %d", resp.StatusCode)
	}
	open := httptest.NewServer(shareServer("").statusHandler())
	defer open.Close()
	if resp, err = http.Get(open.URL + "/status"); err != nil || resp.StatusCode != http.StatusOK {
		t.Fatalf("/status without a configured token: %v %v", err, resp)
	}
	resp.Body.Close()
}
