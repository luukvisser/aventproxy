package webrtc

import (
	"net"
	"strings"

	"github.com/pion/ice/v4"
)

// LAN-only WebRTC: media between the bridge and a camera may only travel over
// the local network. Three layers make that hold without any firewall:
//
//   - no STUN or TURN servers, so the bridge gathers host candidates only and
//     never allocates a relay (the caller leaves ICEServers empty);
//   - the camera's signalled candidates are filtered with LANCandidateAllowed /
//     FilterLANCandidates: only host candidates on a private address survive;
//   - the ICE agent gets IsLANAddress as its remote IP filter (NewServerAPI with
//     Filters.LANOnly), which pion applies to signalled candidates and to the
//     peer-reflexive ones it learns from incoming connectivity checks alike. A
//     check from a private address (the camera answering through the host's
//     NAT, or checking from an address it did not signal) still forms a pair;
//     one from a public address is ignored.

// IsLANAddress reports whether ip is an address that stays on the local
// network: RFC 1918 IPv4, IPv4/IPv6 link-local, or an IPv6 unique local
// address (fc00::/7). Loopback, CGNAT (100.64.0.0/10) and every global address
// are not.
func IsLANAddress(ip net.IP) bool {
	if ip == nil {
		return false
	}
	return ip.IsPrivate() || ip.IsLinkLocalUnicast()
}

// LANCandidateAllowed reports whether a remote ICE candidate line may be used
// on a LAN-only connection: a host candidate whose address is a literal LAN
// address, or a peer-reflexive one on a LAN address. Server-reflexive and relay
// candidates are refused whatever their address (they exist to cross a NAT or
// reach a TURN server), as are mDNS names, which cannot be checked, and
// anything that does not parse. The line may carry the "a=" and "candidate:"
// prefixes. typ is the candidate type for logging ("" when unparsable); it
// never includes the address.
func LANCandidateAllowed(line string) (allowed bool, typ string) {
	raw := strings.TrimSpace(line)
	raw = strings.TrimPrefix(raw, "a=")
	if raw == "" {
		return false, ""
	}
	cand, err := ice.UnmarshalCandidate(raw)
	if err != nil {
		return false, ""
	}
	typ = cand.Type().String()
	switch cand.Type() {
	case ice.CandidateTypeHost, ice.CandidateTypePeerReflexive:
	default:
		return false, typ
	}
	addr, _, _ := strings.Cut(cand.Address(), "%") // an IPv6 zone, fe80::1%eth0
	return IsLANAddress(net.ParseIP(addr)), typ
}

// FilterLANCandidates removes every a=candidate line from an SDP that
// LANCandidateAllowed refuses, and leaves all other lines exactly as they were.
// It returns the new SDP and how many candidate lines were kept and dropped.
func FilterLANCandidates(sdp string) (filtered string, kept, dropped int) {
	lines := strings.SplitAfter(sdp, "\n")
	out := make([]string, 0, len(lines))
	for _, line := range lines {
		if strings.HasPrefix(line, "a=candidate:") {
			if ok, _ := LANCandidateAllowed(line); !ok {
				dropped++
				continue
			}
			kept++
		}
		out = append(out, line)
	}
	return strings.Join(out, ""), kept, dropped
}

// LANCandidateFor is LANCandidateAllowed for a camera whose own LAN addresses
// are known (the integration's lan_hosts for it). A candidate LANCandidateAllowed
// refuses is kept in a rewritten form when it still names one of those
// addresses:
//
//   - a host candidate under an mDNS name (which cannot be resolved from inside
//     a container), or on a public IPv4 address, becomes a host candidate on the
//     camera's address with the same port, when the camera has exactly one
//     known address. A host candidate's port is the camera's own socket; an
//     SCD973-family unit on protocol 3.3 put its public address in its host
//     candidate (02-11-2026: host, srflx and relay all public, none on its LAN
//     address), so this is the only way it streams over the LAN;
//   - a server-reflexive candidate whose base (raddr/rport) is one of the
//     camera's addresses becomes a host candidate on that base: the socket the
//     camera sends from on the LAN.
//
// A camera that put no LAN host candidate in its answer (an SCD973-family unit
// on protocol 3.3, 02-11-2026: kept 0) can so still stream over the LAN. out
// is the line to use, with the input's "candidate:" prefix kept; class says
// what kind of address the input carried, for the log, never the address.
func LANCandidateFor(line string, hosts []string) (out string, allowed bool, typ, class string) {
	if ok, t := LANCandidateAllowed(line); ok {
		return line, true, t, "lan"
	}
	raw := strings.TrimSpace(line)
	raw = strings.TrimPrefix(raw, "a=")
	cand, err := ice.UnmarshalCandidate(raw)
	if err != nil {
		return line, false, "", "unparsable"
	}
	typ = cand.Type().String()
	class = AddressClass(cand.Address())
	own := lanHostSet(hosts)
	if len(own) == 0 || cand.NetworkType().IsTCP() {
		return line, false, typ, class
	}
	var addr string
	var port int
	switch cand.Type() {
	case ice.CandidateTypeHost:
		if (class != "mdns" && class != "public-ipv4") || len(own) != 1 {
			return line, false, typ, class
		}
		for ip := range own {
			addr = ip
		}
		port = cand.Port()
	case ice.CandidateTypeServerReflexive:
		related := cand.RelatedAddress()
		if related == nil || related.Port == 0 || !own[related.Address] {
			return line, false, typ, class
		}
		addr, port = related.Address, related.Port
	default:
		return line, false, typ, class
	}
	host, err := ice.NewCandidateHost(&ice.CandidateHostConfig{
		Network:    cand.NetworkType().NetworkShort(),
		Address:    addr,
		Port:       port,
		Component:  cand.Component(),
		Priority:   cand.Priority(),
		Foundation: cand.Foundation(),
	})
	if err != nil {
		return line, false, typ, class
	}
	out = host.Marshal()
	if strings.HasPrefix(raw, "candidate:") {
		out = "candidate:" + out
	}
	if strings.HasPrefix(strings.TrimSpace(line), "a=") {
		out = "a=" + out
	}
	return out, true, typ, class
}

// lanHostSet is the camera's addresses that are literal LAN addresses.
func lanHostSet(hosts []string) map[string]bool {
	set := map[string]bool{}
	for _, h := range hosts {
		if ip := net.ParseIP(h); ip != nil && IsLANAddress(ip) {
			set[ip.String()] = true
		}
	}
	return set
}

// AddressClass names the kind of address a candidate carries, for logs that
// must not show the address itself.
func AddressClass(addr string) string {
	addr, _, _ = strings.Cut(addr, "%")
	if strings.HasSuffix(strings.ToLower(addr), ".local") {
		return "mdns"
	}
	ip := net.ParseIP(addr)
	switch {
	case ip == nil:
		return "name"
	case ip.IsLoopback():
		return "loopback"
	case IsLANAddress(ip) && ip.To4() != nil:
		return "lan-ipv4"
	case IsLANAddress(ip):
		return "lan-ipv6"
	case ip.To4() != nil:
		return "public-ipv4"
	default:
		return "public-ipv6"
	}
}

// FilterLANCandidatesFor is FilterLANCandidates with LANCandidateFor: a
// candidate that names one of the camera's own addresses is rewritten and
// kept. rewritten counts those.
func FilterLANCandidatesFor(sdp string, hosts []string) (filtered string, kept, dropped, rewritten int) {
	lines := strings.SplitAfter(sdp, "\n")
	out := make([]string, 0, len(lines))
	for _, line := range lines {
		if strings.HasPrefix(line, "a=candidate:") {
			body := strings.TrimRight(line, "\r\n")
			ending := line[len(body):]
			newLine, ok, _, _ := LANCandidateFor(body, hosts)
			if !ok {
				dropped++
				continue
			}
			kept++
			if newLine != body {
				rewritten++
			}
			line = newLine + ending
		}
		out = append(out, line)
	}
	return strings.Join(out, ""), kept, dropped, rewritten
}
