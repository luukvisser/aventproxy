package webrtc

import (
	"net"
	"strings"
	"testing"

	"github.com/pion/webrtc/v4"
)

// Documentation and test addresses only (RFC 5737, RFC 3849); none is real.
func TestIsLANAddress(t *testing.T) {
	for addr, want := range map[string]bool{
		"10.1.2.3":        true,
		"172.16.0.9":      true,
		"172.31.255.1":    true,
		"192.168.1.20":    true,
		"169.254.10.10":   true,
		"fd12:3456::1":    true, // ULA
		"fc00::7":         true, // ULA
		"fe80::1":         true, // link-local
		"172.32.0.1":      false,
		"100.64.0.1":      false, // CGNAT is not the LAN
		"127.0.0.1":       false,
		"::1":             false,
		"198.51.100.7":    false,
		"203.0.113.9":     false,
		"2001:db8::1":     false,
		"0.0.0.0":         false,
		"255.255.255.255": false,
	} {
		if got := IsLANAddress(net.ParseIP(addr)); got != want {
			t.Errorf("IsLANAddress(%s) = %v, want %v", addr, got, want)
		}
	}
	if IsLANAddress(nil) {
		t.Error("nil counted as a LAN address")
	}
}

func TestLANCandidateAllowed(t *testing.T) {
	cases := []struct {
		line string
		want bool
		typ  string
	}{
		{"candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host", true, "host"},
		{"a=candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host", true, "host"},
		{"a=candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host\r\n", true, "host"},
		{"candidate:2 1 udp 2130706431 fd12:3456::1 40000 typ host", true, "host"},
		{"candidate:3 1 udp 2130706431 fe80::1%eth0 40000 typ host", true, "host"},
		{"candidate:4 1 tcp 1518280447 10.0.0.5 9 typ host tcptype active", true, "host"},
		{"candidate:5 1 udp 1862270975 192.168.1.20 40001 typ prflx raddr 0.0.0.0 rport 0", true, "prflx"},
		// A host candidate on a public address.
		{"candidate:6 1 udp 2130706431 198.51.100.7 40000 typ host", false, "host"},
		{"candidate:7 1 udp 2130706431 2001:db8::1 40000 typ host", false, "host"},
		// Reflexive and relayed candidates go, private address or not.
		{"candidate:8 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.20 rport 40000", false, "srflx"},
		{"candidate:9 1 udp 1694498815 192.168.1.20 51000 typ srflx raddr 192.168.1.20 rport 40000", false, "srflx"},
		{"candidate:10 1 udp 16777215 203.0.113.50 3478 typ relay raddr 203.0.113.9 rport 51000", false, "relay"},
		{"candidate:11 1 udp 16777215 10.0.0.1 3478 typ relay raddr 10.0.0.2 rport 51000", false, "relay"},
		{"candidate:12 1 udp 1862270975 198.51.100.7 40001 typ prflx raddr 0.0.0.0 rport 0", false, "prflx"},
		// mDNS names cannot be checked, loopback is not the LAN.
		{"candidate:13 1 udp 2130706431 0b1c3e1a-0000-4000-8000-000000000000.local 40000 typ host", false, "host"},
		{"candidate:14 1 udp 2130706431 127.0.0.1 40000 typ host", false, "host"},
		{"", false, ""},
		{"a=end-of-candidates", false, ""},
		{"candidate:garbage", false, ""},
	}
	for _, c := range cases {
		got, typ := LANCandidateAllowed(c.line)
		if got != c.want || typ != c.typ {
			t.Errorf("LANCandidateAllowed(%q) = %v, %q; want %v, %q", c.line, got, typ, c.want, c.typ)
		}
	}
}

func TestFilterLANCandidates(t *testing.T) {
	sdp := strings.Join([]string{
		"v=0",
		"o=- 1 2 IN IP4 0.0.0.0",
		"s=-",
		"t=0 0",
		"m=video 9 UDP/TLS/RTP/SAVPF 96",
		"c=IN IP4 0.0.0.0",
		"a=candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host",
		"a=candidate:2 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.20 rport 40000",
		"a=candidate:3 1 udp 16777215 203.0.113.50 3478 typ relay raddr 203.0.113.9 rport 51000",
		"a=candidate:4 1 udp 2130706431 fd12:3456::1 40002 typ host",
		"a=candidate:5 1 udp 2130706431 198.51.100.7 40003 typ host",
		"a=end-of-candidates",
		"a=rtpmap:96 H264/90000",
		"",
	}, "\r\n")

	got, kept, dropped := FilterLANCandidates(sdp)
	if kept != 2 || dropped != 3 {
		t.Fatalf("kept %d, dropped %d; want 2, 3", kept, dropped)
	}
	for _, gone := range []string{"203.0.113.9", "203.0.113.50", "198.51.100.7", "srflx", "relay"} {
		if strings.Contains(got, gone) {
			t.Errorf("filtered SDP still has %q:\n%s", gone, got)
		}
	}
	for _, stays := range []string{
		"a=candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host\r\n",
		"a=candidate:4 1 udp 2130706431 fd12:3456::1 40002 typ host\r\n",
		"a=end-of-candidates\r\n",
		"c=IN IP4 0.0.0.0\r\n",
		"a=rtpmap:96 H264/90000\r\n",
	} {
		if !strings.Contains(got, stays) {
			t.Errorf("filtered SDP lost %q", stays)
		}
	}

	// Without candidates to drop the SDP comes back byte for byte.
	clean := "v=0\r\na=candidate:1 1 udp 2130706431 10.0.0.5 40000 typ host\r\na=rtpmap:96 H264/90000\r\n"
	if out, k, d := FilterLANCandidates(clean); out != clean || k != 1 || d != 0 {
		t.Fatalf("clean SDP changed: %q (kept %d, dropped %d)", out, k, d)
	}
}

func TestLANOnlyAPIBuilds(t *testing.T) {
	api, err := NewLANOnlyAPI()
	if err != nil || api == nil {
		t.Fatalf("NewLANOnlyAPI: %v", err)
	}
	pc, err := api.NewPeerConnection(webrtc.Configuration{})
	if err != nil {
		t.Fatal(err)
	}
	defer pc.Close()
	if n := len(pc.GetConfiguration().ICEServers); n != 0 {
		t.Fatalf("%d ICE servers on a LAN-only peer connection", n)
	}
}

func TestLANCandidateFor(t *testing.T) {
	own := []string{"192.168.1.60"}
	cases := []struct {
		name  string
		line  string
		hosts []string
		want  string
		ok    bool
		class string
	}{
		{"a LAN host candidate stays as it is", "candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host", own,
			"candidate:1 1 udp 2130706431 192.168.1.20 40000 typ host", true, "lan"},
		{"an mDNS host candidate lands on the camera's one address", "candidate:2 1 udp 2130706431 0b1c3e1a-0000-4000-8000-000000000000.local 40000 typ host", own,
			"candidate:2 1 udp 2130706431 192.168.1.60 40000 typ host", true, "mdns"},
		{"not when the camera has two addresses", "candidate:2 1 udp 2130706431 0b1c3e1a-0000-4000-8000-000000000000.local 40000 typ host", []string{"192.168.1.60", "192.168.1.61"},
			"", false, "mdns"},
		{"a host candidate on a public address lands on the camera's one address", "candidate:5 1 udp 2130706431 203.0.113.9 40000 typ host", own,
			"candidate:5 1 udp 2130706431 192.168.1.60 40000 typ host", true, "public-ipv4"},
		{"not a public host candidate when the camera has two addresses", "candidate:5 1 udp 2130706431 203.0.113.9 40000 typ host", []string{"192.168.1.60", "192.168.1.61"},
			"", false, "public-ipv4"},
		{"not a public host candidate without a known address", "candidate:5 1 udp 2130706431 203.0.113.9 40000 typ host", nil,
			"", false, "public-ipv4"},
		{"a srflx candidate whose base is the camera's address", "a=candidate:3 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.60 rport 40000", own,
			"a=candidate:3 1 udp 1694498815 192.168.1.60 40000 typ host", true, "public-ipv4"},
		{"not when the base is another address", "candidate:3 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.70 rport 40000", own,
			"", false, "public-ipv4"},
		{"not without a base", "candidate:3 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 0.0.0.0 rport 0", own,
			"", false, "public-ipv4"},
		{"a relay is never the LAN", "candidate:4 1 udp 16777215 203.0.113.50 3478 typ relay raddr 192.168.1.60 rport 40000", own,
			"", false, "public-ipv4"},
		{"without known addresses nothing changes", "candidate:2 1 udp 2130706431 0b1c3e1a-0000-4000-8000-000000000000.local 40000 typ host", nil,
			"", false, "mdns"},
		{"a public address in the list is not a LAN address", "candidate:3 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 198.51.100.7 rport 40000", []string{"198.51.100.7"},
			"", false, "public-ipv4"},
	}
	for _, c := range cases {
		got, ok, _, class := LANCandidateFor(c.line, c.hosts)
		if ok != c.ok || class != c.class || (ok && got != c.want) {
			t.Errorf("%s: LANCandidateFor = %q, %v, %q; want %q, %v, %q", c.name, got, ok, class, c.want, c.ok, c.class)
		}
		if ok {
			if allowed, _ := LANCandidateAllowed(got); !allowed {
				t.Errorf("%s: the rewritten candidate %q is not a LAN candidate", c.name, got)
			}
		}
	}
}

func TestFilterLANCandidatesFor(t *testing.T) {
	sdp := "v=0\r\n" +
		"a=candidate:1 1 udp 2130706431 0b1c3e1a-0000-4000-8000-000000000000.local 40000 typ host\r\n" +
		"a=candidate:2 1 udp 1694498815 203.0.113.9 51000 typ srflx raddr 192.168.1.60 rport 40000\r\n" +
		"a=candidate:3 1 udp 16777215 203.0.113.50 3478 typ relay raddr 203.0.113.9 rport 51000\r\n" +
		"a=end-of-candidates\r\n"
	got, kept, dropped, rewritten := FilterLANCandidatesFor(sdp, []string{"192.168.1.60"})
	if kept != 2 || dropped != 1 || rewritten != 2 {
		t.Fatalf("kept %d dropped %d rewritten %d; want 2, 1, 2", kept, dropped, rewritten)
	}
	want := "v=0\r\n" +
		"a=candidate:1 1 udp 2130706431 192.168.1.60 40000 typ host\r\n" +
		"a=candidate:2 1 udp 1694498815 192.168.1.60 40000 typ host\r\n" +
		"a=end-of-candidates\r\n"
	if got != want {
		t.Errorf("filtered SDP:\n%q\nwant\n%q", got, want)
	}
	// Without the camera's addresses it is FilterLANCandidates.
	plain, k, d := FilterLANCandidates(sdp)
	if same, k2, d2, r2 := FilterLANCandidatesFor(sdp, nil); same != plain || k2 != k || d2 != d || r2 != 0 {
		t.Errorf("FilterLANCandidatesFor(nil) differs from FilterLANCandidates")
	}
}

func TestAddressClass(t *testing.T) {
	for addr, want := range map[string]string{
		"abc.local": "mdns", "192.168.1.5": "lan-ipv4", "fd00::1": "lan-ipv6", "fe80::1%eth0": "lan-ipv6",
		"203.0.113.9": "public-ipv4", "2001:db8::1": "public-ipv6", "127.0.0.1": "loopback", "camera": "name",
	} {
		if got := AddressClass(addr); got != want {
			t.Errorf("AddressClass(%q) = %q; want %q", addr, got, want)
		}
	}
}
