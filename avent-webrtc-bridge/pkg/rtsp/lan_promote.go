package rtsp

import (
	"errors"
	"time"

	"avent-webrtc-bridge/pkg/core"
)

// Back to the LAN after a fallback. A consumer that never disconnects
// (Frigate, Scrypted, an always-on go2rtc) would otherwise stay on the cloud
// for good after one LAN failure, and a freshly booted monitor ignores local
// offers until it has reached Tuya, so every power cut ends there.

// errLANAvailable is why a cloud stream is torn down once the LAN works again.
var errLANAvailable = errors.New("LAN signalling available again")

// armPromotion probes the LAN when the camera's cool-down ends, if the
// cool-down is all that keeps this stream on the cloud. Callers hold cs.mutex.
func (cs *CameraStream) armPromotion() {
	if ok, reason := cs.lanEligible(); !ok && reason != "cooling down" {
		return
	}
	if cs.lanKept {
		// Home Assistant kept a one-session camera's LAN session just now:
		// asking again at once would only take it once more.
		cs.lanKept = false
		core.Logger.Info().Msgf("Trying LAN signalling again for %s in %v", cs.camera.DeviceName, lanCooldownPeriod)
		cs.armPromotionAfter(lanCooldownPeriod)
		return
	}
	s := cs.server
	s.cooldownMutex.Lock()
	delay := time.Until(s.lanCooldowns[cs.camera.DeviceID])
	s.cooldownMutex.Unlock()

	if delay <= 0 { // the cool-down ran out while the cloud was starting
		delay = 0
		core.Logger.Info().Msgf("Trying LAN signalling again for %s now", cs.camera.DeviceName)
	} else {
		core.Logger.Info().Msgf("Trying LAN signalling again for %s in %v", cs.camera.DeviceName, delay.Round(time.Second))
	}
	cs.armPromotionAfter(delay)
}

// armPromotionAfter probes the LAN after delay. Callers hold cs.mutex.
func (cs *CameraStream) armPromotionAfter(delay time.Duration) {
	if cs.promoteTimer != nil {
		cs.promoteTimer.Stop()
	}
	var timer *time.Timer
	timer = time.AfterFunc(delay, func() {
		// Taken before timer is read: armPromotion's caller holds it while timer is set.
		cs.mutex.Lock()
		if cs.promoteTimer != timer { // the stream stopped, or re-armed, since it fired
			cs.mutex.Unlock()
			return
		}
		cs.promoteTimer = nil
		cs.promote()
	})
	cs.promoteTimer = timer
}

// promote runs a throwaway LAN source to see whether the LAN works again,
// without the lock while it waits. When it does, the cloud stream is torn
// down and its clients closed, so they reconnect onto the LAN; when it does
// not, the cool-down starts over. Called with cs.mutex held; releases it.
func (cs *CameraStream) promote() {
	if ok, _ := cs.lanEligible(); !ok { // the cool-down was extended meanwhile
		cs.armPromotion()
		cs.mutex.Unlock()
		return
	}
	// Another stream of the camera (a local-only one) holds its LAN session:
	// a probe beside it would be a second session. Try again later.
	if !cs.acquireLAN() {
		core.Logger.Info().Msgf("LAN session of %s is in use by another stream; trying again in %v", cs.camera.DeviceName, lanCooldownPeriod)
		cs.armPromotionAfter(lanCooldownPeriod)
		cs.mutex.Unlock()
		return
	}
	cs.server.share.flush(cs.camera.DeviceID)
	// A forwarder with no clients: the cloud stream's viewers never see the probe.
	probe := cs.lanFactory(NewRTPForwarder())
	// A throwaway session: the integration must not move its traffic onto it.
	switch src := probe.(type) {
	case *LANSource:
		src.share = nil
	case *exclusiveSource:
		src.share = nil
	}
	stopped, err := cs.runLANAttempt(probe)
	if stopped {
		cs.releaseLAN()
		cs.mutex.Unlock()
		return
	}
	probe.Stop() // before the clients reconnect, so the monitor is free for them
	cs.releaseLAN()
	if errors.Is(err, errLANSessionKept) {
		// Not a LAN failure: the cool-down stays as it is. Try again later.
		core.Logger.Info().Msgf("Home Assistant kept the LAN session of %s; trying again in %v", cs.camera.DeviceName, lanCooldownPeriod)
		cs.armPromotionAfter(lanCooldownPeriod)
		cs.mutex.Unlock()
		return
	}
	if err != nil {
		cs.server.startLANCooldown(cs.camera.DeviceID)
		core.Logger.Info().Msgf("LAN signalling still unavailable for %s: %v", cs.camera.DeviceName, err)
		cs.armPromotion()
		cs.mutex.Unlock()
		return
	}
	cs.server.lanWorked(cs.camera.DeviceID)
	core.Logger.Info().Msgf("%v for %s, moving viewers", errLANAvailable, cs.camera.DeviceName)
	clients := cs.teardownLocked()
	cs.mutex.Unlock()
	closeClients(clients)
}
