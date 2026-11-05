package rtsp

import (
	"bufio"
	"crypto/hmac"
	"crypto/md5"
	"crypto/rand"
	"crypto/sha256"
	"crypto/subtle"
	"encoding/hex"
	"errors"
	"fmt"
	"net"
	"net/url"
	"strconv"
	"strings"
	"time"

	"avent-webrtc-bridge/pkg/core"
)

// RTSP Digest authentication (RFC 2617, MD5; RFC 7616 is the same exchange).
// The stream carries a child's room, so with a password configured
// (BridgeConfig.RTSPPassword) every request needs it: anything that can reach
// the port, the host's own processes included, otherwise gets the video.
// Basic is refused, so the password never crosses the wire. Clients that pull
// the stream (ffmpeg in Home Assistant's stream worker and snapshots, go2rtc)
// send OPTIONS without credentials, get the 401 and retry with them.

// RTSPUser is the user name the integration puts in the stream URL.
const RTSPUser = "homeassistant"

const (
	digestRealm = "avent-webrtc-bridge"
	// nonceLifetime bounds a nonce; an older one is answered with stale=true,
	// and clients retry with the fresh nonce without asking for a password.
	nonceLifetime = 5 * time.Minute
)

// digestAuth checks requests against one user and password. The nonce is
// stateless: its issue time and an HMAC of it under a key made at start, so
// a nonce cannot be made up or kept alive past its lifetime.
type digestAuth struct {
	user, password string
	key            []byte
	now            func() time.Time
}

func newDigestAuth(user, password string) *digestAuth {
	key := make([]byte, 32)
	if _, err := rand.Read(key); err != nil {
		panic(fmt.Sprintf("rtsp: no randomness for the digest nonce key: %v", err))
	}
	return &digestAuth{user: user, password: password, key: key, now: time.Now}
}

func (a *digestAuth) nonce() string {
	ts := strconv.FormatInt(a.now().Unix(), 16)
	return ts + "." + a.nonceMAC(ts)
}

func (a *digestAuth) nonceMAC(ts string) string {
	m := hmac.New(sha256.New, a.key)
	m.Write([]byte(ts))
	return hex.EncodeToString(m.Sum(nil)[:16])
}

// nonceState reports whether a nonce is ours, and whether it is still fresh.
func (a *digestAuth) nonceState(nonce string) (ours, fresh bool) {
	ts, mac, ok := strings.Cut(nonce, ".")
	if !ok || !hmac.Equal([]byte(mac), []byte(a.nonceMAC(ts))) {
		return false, false
	}
	issued, err := strconv.ParseInt(ts, 16, 64)
	if err != nil {
		return false, false
	}
	age := a.now().Sub(time.Unix(issued, 0))
	return true, age >= -time.Minute && age <= nonceLifetime
}

// challenge is the WWW-Authenticate value of a 401.
func (a *digestAuth) challenge(stale bool) string {
	c := fmt.Sprintf(`Digest realm="%s", nonce="%s", algorithm=MD5`, digestRealm, a.nonce())
	if stale {
		c += ", stale=true"
	}
	return c
}

// check reports whether the request carries valid credentials, and whether
// they were valid apart from an expired nonce.
func (a *digestAuth) check(method, requestURI, header string) (ok, stale bool) {
	scheme, rest, _ := strings.Cut(strings.TrimSpace(header), " ")
	if !strings.EqualFold(scheme, "Digest") {
		return false, false
	}
	p := parseDigestParams(rest)
	if alg := p["algorithm"]; alg != "" && !strings.EqualFold(alg, "MD5") {
		return false, false
	}
	if p["realm"] != digestRealm || !sameRequestURI(p["uri"], requestURI) {
		return false, false
	}
	ha1 := md5hex(a.user + ":" + digestRealm + ":" + a.password)
	ha2 := md5hex(method + ":" + p["uri"])
	var want string
	switch p["qop"] {
	case "":
		want = md5hex(ha1 + ":" + p["nonce"] + ":" + ha2)
	case "auth":
		if p["nc"] == "" || p["cnonce"] == "" {
			return false, false
		}
		want = md5hex(ha1 + ":" + p["nonce"] + ":" + p["nc"] + ":" + p["cnonce"] + ":auth:" + ha2)
	default:
		return false, false
	}
	userOK := subtle.ConstantTimeCompare([]byte(p["username"]), []byte(a.user)) == 1
	respOK := subtle.ConstantTimeCompare([]byte(strings.ToLower(p["response"])), []byte(want)) == 1
	if !userOK || !respOK {
		return false, false
	}
	ours, fresh := a.nonceState(p["nonce"])
	if !ours {
		return false, false
	}
	return fresh, !fresh
}

// sameRequestURI accepts the digest's uri when it names the request's URI,
// absolute or as its path: clients differ in which they put there.
func sameRequestURI(digestURI, requestURI string) bool {
	if digestURI == "" {
		return false
	}
	if digestURI == requestURI {
		return true
	}
	d, err1 := url.Parse(digestURI)
	r, err2 := url.Parse(requestURI)
	if err1 != nil || err2 != nil {
		return false
	}
	return d.EscapedPath() == r.EscapedPath() && (d.Host == "" || d.Host == r.Host)
}

// parseDigestParams reads the comma-separated key=value list of a Digest
// header; values may be quoted, and a quoted value may hold commas.
func parseDigestParams(s string) map[string]string {
	out := map[string]string{}
	for len(s) > 0 {
		s = strings.TrimLeft(s, " ,")
		eq := strings.IndexByte(s, '=')
		if eq <= 0 {
			break
		}
		key := strings.ToLower(strings.TrimSpace(s[:eq]))
		s = s[eq+1:]
		var val string
		if strings.HasPrefix(s, `"`) {
			end := strings.IndexByte(s[1:], '"')
			if end < 0 {
				break
			}
			val, s = s[1:end+1], s[end+2:]
		} else {
			end := strings.IndexByte(s, ',')
			if end < 0 {
				end = len(s)
			}
			val, s = strings.TrimSpace(s[:end]), s[end:]
		}
		out[key] = val
	}
	return out
}

// DefaultMaxConnections is the RTSP connection cap: Home Assistant opens a
// few per camera (its stream worker, go2rtc, a snapshot's ffmpeg), and each
// connection may hold a camera stream and one of the camera's few LAN
// sessions, which its parent unit needs too.
const DefaultMaxConnections = 16

const (
	// maxUnauthorized is how many requests a connection may send without
	// valid credentials before it is closed: a client asks once without, then
	// with them, and once more after a stale nonce.
	maxUnauthorized = 3
	// authDeadline bounds the time to the first authorized request; without a
	// password the first request has requestReadTimeout, as before.
	authDeadline = 10 * time.Second
)

// errNoCredentials: a connection sent maxUnauthorized requests without valid
// credentials, which firstAuthorizedRequest has logged.
var errNoCredentials = errors.New("no valid credentials")

// header reads a request header regardless of case.
func (r *RTSPRequest) header(name string) string {
	if v, ok := r.Headers[name]; ok {
		return v
	}
	for k, v := range r.Headers {
		if strings.EqualFold(k, name) {
			return v
		}
	}
	return ""
}

// authorize reports whether the request may be served. Without a password
// every request may; otherwise a request without valid credentials gets a 401
// with a fresh challenge.
func (s *RTSPServer) authorize(conn net.Conn, request *RTSPRequest) bool {
	if s.auth == nil {
		return true
	}
	ok, stale := s.auth.check(request.Method, request.URL, request.header("Authorization"))
	if ok {
		return true
	}
	headers := map[string]string{
		"CSeq":             strconv.Itoa(request.CSeq),
		"WWW-Authenticate": s.auth.challenge(stale),
	}
	sendRTSPResponse(conn, 401, "Unauthorized", headers, "")
	return false
}

// firstAuthorizedRequest reads requests until one is authorized, answering
// the others with a challenge, within authDeadline and maxUnauthorized. The
// caller has set the read deadline for the first request.
func (s *RTSPServer) firstAuthorizedRequest(conn net.Conn, reader *bufio.Reader) (*RTSPRequest, error) {
	if s.auth != nil {
		if err := conn.SetReadDeadline(time.Now().Add(authDeadline)); err != nil {
			return nil, err
		}
	}
	for refused := 0; ; refused++ {
		request, err := s.parseRTSPRequestFromReader(reader)
		if err != nil {
			return nil, err
		}
		if s.authorize(conn, request) {
			return request, nil
		}
		if refused+1 >= maxUnauthorized {
			core.Logger.Warn().Msgf("RTSP: closing a connection after %d requests without valid credentials", maxUnauthorized)
			return nil, errNoCredentials
		}
	}
}

func md5hex(s string) string {
	sum := md5.Sum([]byte(s))
	return hex.EncodeToString(sum[:])
}
