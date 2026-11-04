package rtsp

import (
	"context"
	"errors"
	"fmt"
	"net/url"
	"strings"
	"sync"
	"time"

	"avent-webrtc-bridge/pkg/core"
	"avent-webrtc-bridge/pkg/storage"

	tuyalan "github.com/luukvisser/tuya-lan"
)

// StreamMode picks the path a stream takes. Every camera is served on four
// RTSP paths, each its own stream:
//
//	/<camera>               auto: the LAN first, the cloud as fallback (startStream)
//	/<camera>/local         the LAN only; never MQTT, never WebRTC
//	/<camera>/webrtc        WebRTC only, set up through Tuya's cloud; no LAN attempt
//	/<camera>/webrtc-first  WebRTC first, the LAN when WebRTC does not start or
//	                        sends no video within webrtcFirstVideoDeadline;
//	                        without a LAN path it is the webrtc mode
//
// The mode segment may sit before or after a resolution segment (/hd, /sd)
// and before a track segment (/video, /audio, /backchannel). The camera is
// always the first segment: SanitizeRTSPPath replaces every "/" in a name, so
// a camera named "local" is "/local" and its local stream "/local/local".
type StreamMode string

const (
	ModeAuto   StreamMode = ""
	ModeLocal  StreamMode = "local"
	ModeWebRTC StreamMode = "webrtc"
	// ModeWebRTCFirst keeps no memory of its own: every stream tries WebRTC
	// first, so a cloud outage costs each new stream one cloud attempt plus
	// webrtcFirstVideoDeadline before the LAN takes over.
	ModeWebRTCFirst StreamMode = "webrtc-first"
)

func (m StreamMode) String() string {
	if m == ModeAuto {
		return "auto"
	}
	return string(m)
}

const (
	// localOnlyDeadline bounds the LAN attempt of a stream with nothing to
	// fall back to (local, and webrtc-first after WebRTC): the handshake cap
	// of pkg/lan, rather than lanFirstFrameDeadline, which is short because
	// the cloud is waiting behind it.
	localOnlyDeadline = 20 * time.Second
	// webrtcFirstVideoDeadline is how long a webrtc-first stream's cloud
	// session, once up, may take to deliver its first video packet before
	// the LAN takes over. A working camera sends one within a second or two
	// (cloudMediaTimeout).
	webrtcFirstVideoDeadline = 5 * time.Second
)

var (
	// errLocalUnavailable answers a /local request for a camera that cannot
	// stream over the LAN (see lanPath): 404.
	errLocalUnavailable = errors.New("no local stream path for this camera")
	// errLANBusy answers a /local request while another stream of the same
	// camera holds its LAN session: 503.
	errLANBusy = errors.New("the camera's LAN session is in use by another stream")
	// errWebRTCOff answers a /webrtc request while WebRTC is off: 404.
	errWebRTCOff = errors.New("WebRTC is turned off")
)

// allowedMode is the mode a request is served in. With WebRTC off only the
// local stream is left: auto and webrtc-first are served as local, so they
// share its stream and its 404 or 503, and webrtc is refused.
func (s *RTSPServer) allowedMode(mode StreamMode) (StreamMode, error) {
	if !s.NoWebRTC {
		return mode, nil
	}
	if mode == ModeWebRTC {
		return mode, errWebRTCOff
	}
	return ModeLocal, nil
}

// streamRequest is what an RTSP URL asks for.
type streamRequest struct {
	path       string
	resolution string
	mode       StreamMode
}

// parseStreamPath splits an RTSP URL into the camera path, the resolution and
// the mode. A URL with segments it does not know keeps its whole path, so the
// camera lookup fails with a 404 as it always did.
func parseStreamPath(rtspURL string) streamRequest {
	parsed, err := url.Parse(rtspURL)
	if err != nil {
		return streamRequest{}
	}
	full := strings.TrimRight(parsed.Path, "/")
	segs := strings.Split(strings.TrimPrefix(full, "/"), "/")
	if full == "" || segs[0] == "" {
		return streamRequest{}
	}
	req := streamRequest{path: "/" + segs[0], resolution: "hd", mode: ModeAuto}
	rest := segs[1:]
	if n := len(rest); n > 0 {
		switch rest[n-1] {
		case "video", "audio", "backchannel":
			rest = rest[:n-1]
		}
	}
	sawRes, sawMode := false, false
	for _, seg := range rest {
		switch {
		case (seg == "hd" || seg == "sd") && !sawRes:
			req.resolution, sawRes = seg, true
		case (seg == string(ModeLocal) || seg == string(ModeWebRTC) || seg == string(ModeWebRTCFirst)) && !sawMode:
			req.mode, sawMode = StreamMode(seg), true
		default:
			return streamRequest{path: full, resolution: "hd", mode: ModeAuto}
		}
	}
	return req
}

// modeEndpoints lists a camera's mode paths for the endpoints log. The local
// one is offered only for a camera that can stream over the LAN, the WebRTC
// ones only while WebRTC is on.
func modeEndpoints(baseURL string, lanCapable, webrtc bool) []string {
	var out []string
	if lanCapable {
		out = append(out, baseURL+"/"+string(ModeLocal)+" local network only")
	}
	if !webrtc {
		return out
	}
	out = append(out, baseURL+"/"+string(ModeWebRTC)+" WebRTC only")
	if lanCapable {
		out = append(out, baseURL+"/"+string(ModeWebRTCFirst)+" WebRTC first, local stream as fallback")
	}
	return out
}

// streamKey is the server's map key. The auto key is the one the bridge has
// always used; the other modes get their own, so an auto, a local and a cloud
// stream of one camera never share a session.
func streamKey(deviceID, resolution string, mode StreamMode) string {
	if mode == ModeAuto {
		return fmt.Sprintf("%s-%s", deviceID, resolution)
	}
	return fmt.Sprintf("%s-%s-%s", deviceID, resolution, mode)
}

// webrtcResolution is the resolution a WebRTC stream asks for. A camera whose
// local stream is set to standard (lan_clarity 2) streams over WebRTC at "sd"
// too, so the cloud fallback costs the camera no more than the local path;
// one set to HD keeps "hd". The request's own resolution still names the
// stream (its key, the LAN's "sd stream" rule).
func (s *RTSPServer) webrtcResolution(deviceID, resolution string) string {
	if s == nil || resolution != "hd" {
		return resolution
	}
	if target, ok := s.LANTargets[deviceID]; ok && target.Clarity == tuyalan.ClarityStandard {
		return "sd"
	}
	return resolution
}

// lanSessions gives one stream at a time a camera's LAN session, whatever its
// mode: whether a monitor accepts two local sessions from one client is
// unknown, and a second one could knock the first off. It is also the
// bridge's share of the monitor's three LAN sessions: one here, at most one
// from the integration, the third left for the parent unit (02-11-2026).
type lanSessions struct {
	mu      sync.Mutex
	holders map[string]*CameraStream // device id -> the stream holding its LAN session
}

func (r *lanSessions) acquire(deviceID string, cs *CameraStream) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.holders == nil {
		r.holders = map[string]*CameraStream{}
	}
	if holder, ok := r.holders[deviceID]; ok && holder != cs {
		return false
	}
	r.holders[deviceID] = cs
	return true
}

func (r *lanSessions) release(deviceID string, cs *CameraStream) {
	r.mu.Lock()
	defer r.mu.Unlock()
	if r.holders[deviceID] == cs {
		delete(r.holders, deviceID)
	}
}

func (r *lanSessions) busy(deviceID string) bool {
	r.mu.Lock()
	defer r.mu.Unlock()
	_, ok := r.holders[deviceID]
	return ok
}

// acquireLAN claims the camera's LAN session for this stream.
func (cs *CameraStream) acquireLAN() bool {
	return cs.server.lanSessions.acquire(cs.camera.DeviceID, cs)
}

// releaseLAN gives up this stream's claim on the camera's LAN session, if it
// has one.
func (cs *CameraStream) releaseLAN() {
	if cs.server != nil {
		cs.server.lanSessions.release(cs.camera.DeviceID, cs)
	}
}

// startLocal serves a local-only stream: one LAN attempt, and when it fails
// the clients are closed so the RTSP request ends instead of waiting for
// media that will not come. It never touches MQTT or the WebRTC bridge, and a
// failure here does not start the camera's cool-down: the auto stream keeps
// trying the LAN.
func (cs *CameraStream) startLocal(gen uint64) {
	cs.mutex.Lock()
	if cs.superseded(gen) {
		cs.mutex.Unlock()
		return
	}
	core.Logger.Info().Msgf("Starting local-only stream for camera: %s", cs.camera.DeviceName)
	cs.runLocal()
}

// runLocal is the LAN attempt of a stream with nothing to fall back to.
// Called with cs.mutex held; releases it.
func (cs *CameraStream) runLocal() {
	fail := func(reason string) {
		core.Logger.Warn().Msgf("Local stream for %s could not start: %s; it does not fall back to the cloud", cs.camera.DeviceName, reason)
		clients := cs.teardownLocked()
		cs.mutex.Unlock()
		closeClients(clients)
	}
	if ok, reason := cs.server.lanPath(cs.camera.DeviceID, cs.resolution); !ok {
		fail(reason)
		return
	}
	if !cs.acquireLAN() {
		fail(errLANBusy.Error())
		return
	}
	source := cs.lanFactory(cs.forwarder)
	cs.lan = source
	source.SetOnError(func(err error) { cs.lanDied(source, err) })
	stopped, err := cs.runAttempt(source, localOnlyDeadline)
	if stopped {
		cs.mutex.Unlock()
		return
	}
	if err != nil {
		cs.lan = nil
		source.Stop()
		cs.releaseLAN()
		fail(err.Error())
		return
	}
	cs.markActive()
	if len(cs.clients) == 0 { // the last client left during the attempt
		cs.scheduleShutdown()
	}
	cs.mutex.Unlock()
	core.Logger.Info().Msgf("Streaming %s locally (LAN only)", cs.camera.DeviceName)
}

// startCloudFirst serves a webrtc-first stream whose camera has a LAN path:
// one cloud attempt, which must deliver video within webrtcFirstVideoDeadline,
// then the LAN, with nothing after it.
func (cs *CameraStream) startCloudFirst(gen uint64) {
	cs.mutex.Lock()
	if cs.superseded(gen) {
		cs.mutex.Unlock()
		return
	}
	name := cs.camera.DeviceName
	core.Logger.Info().Msgf("Starting webrtc-first stream for camera: %s", name)
	if cs.bridgeStarted {
		cs.replaceBridge()
	}
	cs.bridgeStarted = true
	probe := make(chan error, 1)
	cs.cloudProbe = probe
	err := cs.cloudStart()
	if err == nil {
		bridge := cs.webrtcBridge
		ctx, cancel := context.WithTimeout(context.Background(), webrtcFirstVideoDeadline)
		cs.attemptCancel = cancel // a Stop ends the wait
		cs.mutex.Unlock()
		err = waitCloudVideo(ctx, bridge, probe)
		cancel()
		cs.mutex.Lock()
		cs.cloudProbe = nil
		if cs.superseded(gen) {
			cs.mutex.Unlock()
			return
		}
		cs.attemptCancel = nil
		if err == nil {
			cs.markActive()
			if len(cs.clients) == 0 {
				cs.scheduleShutdown()
			}
			cs.armMediaCheck()
			cs.mutex.Unlock()
			core.Logger.Info().Msgf("Streaming %s with cloud signalling (webrtc-first)", name)
			return
		}
		core.Logger.Warn().Msgf("Cloud stream of %s sent no video within %v (%v), falling back to the LAN", name, webrtcFirstVideoDeadline, err)
	} else {
		cs.cloudProbe = nil
		core.Logger.Warn().Err(err).Msgf("Cloud stream of %s did not start, falling back to the LAN", name)
	}
	// The cloud session goes, and the LAN is a new source on the same clients.
	cs.replaceBridge()
	cs.forwarder.ResetMedia()
	cs.runLocal()
}

// waitCloudVideo waits for the bridge's first video packet, a WebRTC error,
// or the end of ctx.
func waitCloudVideo(ctx context.Context, bridge *WebRTCBridge, probe <-chan error) error {
	select {
	case <-bridge.firstVideo:
		return nil
	case err := <-probe:
		return err
	case <-ctx.Done():
		return ctx.Err()
	}
}

// armLimit starts the stream's MaxStreamDuration, once per run. Callers hold
// cs.mutex.
func (cs *CameraStream) armLimit() {
	if cs.server == nil || cs.server.MaxStreamDuration <= 0 || cs.limitTimer != nil {
		return
	}
	cs.limitGen++
	gen := cs.limitGen
	cs.limitTimer = time.AfterFunc(cs.server.MaxStreamDuration, func() { cs.endAtLimit(gen) })
}

// endAtLimit ends the stream at MaxStreamDuration, clients and all, the way a
// WebRTC error does: they reconnect, or, like a dashboard that starts live
// only on a tap, do not. A timer that fired while waiting for the lock, as
// the stream stopped and started again, belongs to the earlier run (gen) and
// ends nothing.
func (cs *CameraStream) endAtLimit(gen uint64) {
	cs.mutex.Lock()
	if !cs.active.Load() || cs.limitTimer == nil || gen != cs.limitGen {
		cs.mutex.Unlock()
		return
	}
	cs.limitTimer = nil
	core.Logger.Info().Msgf("Ending the stream for camera %s after %v (max_stream_minutes)", cs.camera.DeviceName, cs.server.MaxStreamDuration)
	clients := cs.teardownLocked()
	cs.mutex.Unlock()
	closeClients(clients)
}

// lanCapable reports whether a camera can stream this resolution over the
// LAN, for the endpoints log.
func (s *RTSPServer) lanCapable(camera *storage.CameraInfo, resolution string) bool {
	ok, _ := s.lanPath(camera.DeviceID, resolution)
	return ok
}
