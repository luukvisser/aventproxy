package rtsp

import (
	"context"
	"errors"
	"strings"
	"time"

	"avent-webrtc-bridge/pkg/core"
)

// The local stream over Tuya 3.3. Such a camera serves one LAN session at a
// time: an SCD973-family unit accepted the bridge's TCP connection while the
// integration's session was open but answered nothing on it (02-11-2026),
// where the 3.4 SCD951 serves three. So the integration releases its session
// before the bridge connects (share.go), and the bridge never connects
// without that release. 3.3 proves no key in its handshake; tuya-lan proves
// the camera by its first answer before the offer, and the bridge tries 3.3
// only on the one address bound to the camera (lanPath, boundOnly).

// errLANSessionKept: the integration did not release a one-session camera's
// LAN session, so the bridge did not connect. Not a failure of the path.
var errLANSessionKept = errors.New("Home Assistant kept the camera's one LAN session")

// lanExclusiveStartDeadline bounds a 3.3 start once the session is released:
// the handshake, the offer, ICE and the first frame. Measured on the
// SCD973-family unit: first video 0.96 s after the start, the offer answered
// without the version header. tuya-lan sends it once more with the header
// when the first goes unanswered for its 8 s answer wait, which
// lanFirstFrameDeadline would cut off. A variable for tests.
var lanExclusiveStartDeadline = 12 * time.Second

// lanExclusiveAttemptDeadline is the whole attempt on a one-session camera:
// the wait for the release, then the start.
func lanExclusiveAttemptDeadline() time.Duration {
	return shareReleaseWait + lanExclusiveStartDeadline
}

// Exclusive reports whether the camera serves one LAN session at a time.
func (t LANTarget) Exclusive() bool {
	return strings.TrimSpace(t.Protocol) == "3.3"
}

// boundOnly reports whether the target names exactly one address in Hosts,
// with IP empty or the same: how the integration sends a 3.3 camera's bound
// address. A 3.3 camera without a binding gets no LAN credentials from it.
func (t LANTarget) boundOnly() bool {
	return len(t.Hosts) == 1 && t.Hosts[0] != "" && (t.IP == "" || t.IP == t.Hosts[0])
}

// exclusiveSource is a LANSource for a one-session camera: Start first gets
// the integration's session, and keeps wants_session up until it returns, so
// the integration does not take the session back from a slow start.
type exclusiveSource struct {
	*LANSource
	server *RTSPServer
}

func (e *exclusiveSource) Start(ctx context.Context) error {
	id, name := e.deviceID, e.deviceName
	if !e.server.requestLANSession(ctx, id, name) {
		return errLANSessionKept
	}
	defer e.server.share.holdForStart(id)()
	start, cancel := context.WithTimeout(ctx, lanExclusiveStartDeadline)
	defer cancel()
	err := e.LANSource.Start(start)
	if err != nil && ctx.Err() == nil {
		e.server.noteExclusiveSilence(id, name)
	}
	return err
}

// requestLANSession asks the integration for a one-session camera's LAN
// session before a start (share.go). Without an API token there is no
// integration to ask, and the bridge does not connect.
func (s *RTSPServer) requestLANSession(ctx context.Context, id, name string) bool {
	if s.APIToken == "" {
		core.Logger.Info().Msgf("%s serves one LAN session and Home Assistant cannot be asked to release its own (no API token): not connecting", name)
		return false
	}
	asked := time.Now()
	core.Logger.Info().Msgf("Asking Home Assistant to release its LAN session to %s (the camera serves one at a time)", name)
	if !s.share.requestSession(ctx, id, shareReleaseWait) {
		core.Logger.Info().Msgf("Home Assistant did not release its LAN session to %s within %s: not connecting", name, shareReleaseWait)
		return false
	}
	core.Logger.Info().Msgf("Home Assistant released its LAN session to %s after %s", name, time.Since(asked).Round(time.Millisecond))
	return true
}

// noteExclusiveSilence logs, once per camera and run, that a one-session camera
// did not take the bridge's session even with the integration's closed: then
// the one-session explanation was wrong.
func (s *RTSPServer) noteExclusiveSilence(id, name string) {
	if _, seen := s.exclusiveSilence.LoadOrStore(id, true); seen {
		return
	}
	core.Logger.Warn().Msgf("The camera %s did not answer the bridge's 3.3 session even with ours closed", name)
}
