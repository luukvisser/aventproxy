package rtsp

import (
	"fmt"
	"sort"
	"strings"
	"time"

	"avent-webrtc-bridge/pkg/core"
)

// Audio that goes missing leaves no trace by itself: every stage between the
// camera and the player carries on with an empty stream, and the video keeps
// flowing. On 04-11-2026 both cameras were silent for a few minutes in the
// Home Assistant app with nothing in any log. This file counts the audio at
// each stage the bridge owns and says so, once, when one of them stops while
// the video flows:
//
//	camera        PCM frames from the monitor (LAN) or RTP packets (cloud)
//	G.711         packets written to one RTSP client's G.711 track
//	AAC copy      frames ffmpeg produced (aac.go)
//	AAC           frames written to one RTSP client's AAC track
//
// What lies past the bridge (go2rtc's own ffmpeg to Opus, the browser) is not
// visible from here; the counts then show the bridge did deliver.

const (
	// audioSilentAfter is how long a stage may deliver nothing while video
	// flows before it counts as silent. aacMaxLag (250 ms) is the longest
	// gap the code treats as an ordinary pause, and a warning that low would
	// fire on pauses it expects; 20 times that is far beyond jitter and a
	// transcoder's start, and short enough that a silent minute shows in the
	// log. Nothing else notices: a LAN session ends only when all its media
	// stops (pkg/lan's mediaIdleTimeout), not when only the audio does. Unverified: whether a camera
	// sends nothing at all in a quiet room (tuya-lan's reader allows for
	// "audio paused"); if one does, "camera sends no audio" will say so.
	audioSilentAfter = 20 * aacMaxLag
	// audioCheckEvery spaces the checks, which ride on the video packets.
	audioCheckEvery = time.Second
)

// audioNow is the time source of the audio accounting; tests replace it.
var audioNow = time.Now

// audioStage counts the audio at one stage and notices when it stops. Callers
// hold rf.mutex.
type audioStage struct {
	count  uint64
	last   time.Time // the last audio; zero before the first
	armed  time.Time // since when audio is expected here; zero until first checked
	gapMax time.Duration
	silent bool
}

// ref is the moment the stage's silence is measured from.
func (s *audioStage) ref() time.Time {
	if s.last.After(s.armed) {
		return s.last
	}
	return s.armed
}

// note counts one unit of audio. It returns how long the stage was silent
// when this ends a silence that was reported, else 0.
func (s *audioStage) note(now time.Time) time.Duration {
	var back time.Duration
	if ref := s.ref(); !ref.IsZero() {
		gap := now.Sub(ref)
		if gap > s.gapMax {
			s.gapMax = gap
		}
		if s.silent {
			back = gap
		}
	}
	s.silent = false
	s.count++
	s.last = now
	return back
}

// idle is how long the stage has delivered nothing, counted from the later of
// its last audio, the moment it was armed and floor (an upstream stage's
// restart, which no downstream stage can be blamed for).
func (s *audioStage) idle(now, floor time.Time) time.Duration {
	ref := s.ref()
	if floor.After(ref) {
		ref = floor
	}
	if ref.IsZero() {
		return 0
	}
	return now.Sub(ref)
}

// longest is the longest gap so far, including one that is still open.
func (s *audioStage) longest(now time.Time) time.Duration {
	gap := s.gapMax
	if ref := s.ref(); !ref.IsZero() && now.Sub(ref) > gap {
		gap = now.Sub(ref)
	}
	return gap
}

// audioWatch is a forwarder's audio accounting.
type audioWatch struct {
	started bool // anything counted or checked; a summary is worth a line
	camera  audioStage
	// cameraBack is when the camera's audio last resumed after a reported
	// silence; the stages after it are measured from there.
	cameraBack time.Time
	ignored    uint64 // camera frames the bridge could not convert
	made       uint64 // G.711 packets handed to the forwarder
	aacOut     audioStage
	checkedAt  time.Time
}

// NoteCameraAudio counts one unit of audio from the camera, before anything
// is done with it. ignored: the bridge could not use it (an unsupported
// format), so no G.711 follows.
func (rf *RTPForwarder) NoteCameraAudio(ignored bool) {
	rf.mutex.Lock()
	defer rf.mutex.Unlock()
	now := audioNow()
	w := &rf.audio
	w.started = true
	if ignored {
		w.ignored++
	}
	if back := w.camera.note(now); back > 0 {
		w.cameraBack = now
		rf.logAudioBack("camera audio", back)
	}
}

// label names the stream in the audio log lines.
func (rf *RTPForwarder) label() string {
	if rf.AACLabel == "" {
		return "stream"
	}
	return rf.AACLabel
}

// shortSession is the part of a session id the audio lines and /status show.
func shortSession(id string) string {
	if len(id) > 8 {
		return id[:8]
	}
	return id
}

func (rf *RTPForwarder) logAudioBack(what string, silent time.Duration) {
	core.Logger.Info().Msgf("Audio of %s: %s is back after %s", rf.label(), what, silent.Round(time.Second))
}

// wantsG711 reports whether the client set up the G.711 track; the same test
// send applies before writing to it.
func (c *RTPClient) wantsG711() bool {
	if c.transportMode == TransportUDP {
		return c.audioConn != nil
	}
	return c.tcpConn != nil && c.audioRTPChannel != c.videoRTPChannel
}

// maybeCheckAudioLocked runs checkAudioLocked at most once per
// audioCheckEvery. Callers hold rf.mutex.
func (rf *RTPForwarder) maybeCheckAudioLocked(now time.Time) {
	if now.Sub(rf.audio.checkedAt) < audioCheckEvery {
		return
	}
	rf.audio.checkedAt = now
	rf.checkAudioLocked(now)
}

// checkAudioLocked warns, once per silent period, about a stage that has
// delivered no audio for audioSilentAfter. It runs while video flows and the
// stream has clients. Stages are checked in the order the audio travels, and
// a silent stage hides those after it, which are silent as a consequence: the
// line names the first one. Callers hold rf.mutex.
func (rf *RTPForwarder) checkAudioLocked(now time.Time) {
	if len(rf.clients) == 0 {
		return
	}
	w := &rf.audio
	w.started = true
	if w.camera.armed.IsZero() {
		w.camera.armed = now
	}
	if rf.flagSilent(&w.camera, now, time.Time{}, func(idle time.Duration) string {
		return fmt.Sprintf("camera sends no audio for %s while video flows (%d frames so far, %d ignored)",
			idle.Round(time.Second), w.camera.count, w.ignored)
	}) {
		return
	}
	floor := w.cameraBack

	ids := make([]string, 0, len(rf.clients))
	for id := range rf.clients {
		ids = append(ids, id)
	}
	sort.Strings(ids)

	encoderSilent := false
	if rf.aac != nil {
		if w.aacOut.armed.IsZero() {
			w.aacOut.armed = now
		}
		encoderSilent = rf.flagSilent(&w.aacOut, now, floor, func(idle time.Duration) string {
			return fmt.Sprintf("AAC copy silent for %s while the camera's audio flows (%d frames so far)",
				idle.Round(time.Second), w.aacOut.count)
		})
	} else {
		w.aacOut.armed, w.aacOut.silent = time.Time{}, false
	}

	for _, id := range ids {
		client := rf.clients[id]
		if client.wantsG711() {
			if client.g711.armed.IsZero() {
				client.g711.armed = now
			}
			rf.flagSilent(&client.g711, now, floor, func(idle time.Duration) string {
				return fmt.Sprintf("no G.711 to client %s for %s while the camera's audio flows (%d packets so far)",
					shortSession(id), idle.Round(time.Second), client.g711.count)
			})
		}
		if client.aacOn && rf.aac != nil && !encoderSilent {
			if client.aac.armed.IsZero() {
				client.aac.armed = now
			}
			aacFloor := floor
			if w.aacOut.armed.After(aacFloor) {
				aacFloor = w.aacOut.armed
			}
			rf.flagSilent(&client.aac, now, aacFloor, func(idle time.Duration) string {
				return fmt.Sprintf("no AAC to client %s for %s while the AAC copy runs (%d frames so far)",
					shortSession(id), idle.Round(time.Second), client.aac.count)
			})
		}
	}
}

// flagSilent logs the warning the first time a stage has been idle for
// audioSilentAfter, and reports whether it is silent now.
func (rf *RTPForwarder) flagSilent(s *audioStage, now, floor time.Time, msg func(idle time.Duration) string) bool {
	idle := s.idle(now, floor)
	if idle <= audioSilentAfter {
		return s.silent
	}
	if !s.silent {
		s.silent = true
		core.Logger.Warn().Msgf("Audio of %s: %s", rf.label(), msg(idle))
	}
	return true
}

// logAudioSummaryLocked writes the one line a stream's audio leaves in the log
// when it stops, and resets the counts. Callers hold rf.mutex.
func (rf *RTPForwarder) logAudioSummaryLocked(now time.Time) {
	w := &rf.audio
	if !w.started {
		return
	}
	var silent []string
	if w.camera.silent {
		silent = append(silent, "camera")
	}
	aac := "off"
	if rf.AACFFmpeg != "" {
		aac = fmt.Sprintf("%d frames, longest gap %s", w.aacOut.count, w.aacOut.longest(now).Round(time.Millisecond))
		if w.aacOut.silent {
			silent = append(silent, "AAC copy")
		}
	}
	ids := make([]string, 0, len(rf.clients))
	for id := range rf.clients {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	clients := make([]string, 0, len(ids))
	for _, id := range ids {
		c := rf.clients[id]
		part := shortSession(id) + " G.711 "
		if c.wantsG711() {
			part += fmt.Sprintf("%d (gap %s)", c.g711.count, c.g711.longest(now).Round(time.Millisecond))
		} else {
			part += "none"
		}
		if c.aacOn {
			part += fmt.Sprintf(", AAC %d (gap %s)", c.aac.count, c.aac.longest(now).Round(time.Millisecond))
		} else {
			part += ", AAC none"
		}
		if c.g711.silent {
			silent = append(silent, "G.711 to "+shortSession(id))
		}
		if c.aac.silent {
			silent = append(silent, "AAC to "+shortSession(id))
		}
		clients = append(clients, part)
	}
	line := fmt.Sprintf("Audio of %s over: camera %d frames (%d ignored, longest gap %s), %d G.711 packets made; AAC copy %s; clients: %s",
		rf.label(), w.camera.count, w.ignored, w.camera.longest(now).Round(time.Millisecond), w.made, aac, strings.Join(clients, "; "))
	if len(clients) == 0 {
		line = strings.TrimSuffix(line, "; clients: ") + "; no clients"
	}
	if len(silent) > 0 {
		line += "; silent at the end: " + strings.Join(silent, ", ")
	}
	core.Logger.Info().Msg(line)
	rf.audio = audioWatch{}
}

// AudioStageStatus is one stage's counts in /status. LastAgeS is the seconds
// since its last audio, absent when it has had none.
type AudioStageStatus struct {
	Count       uint64   `json:"count"`
	LastAgeS    *float64 `json:"last_age_s,omitempty"`
	LongestGapS float64  `json:"longest_gap_s"`
	Silent      bool     `json:"silent"`
}

// AudioClientStatus is what one RTSP client gets. A track the client did not
// set up is absent.
type AudioClientStatus struct {
	Session string            `json:"session"`
	G711    *AudioStageStatus `json:"g711,omitempty"`
	AAC     *AudioStageStatus `json:"aac,omitempty"`
}

// AudioStatus is a stream's audio accounting in /status: how much audio each
// stage has seen and how long ago, to read during a silent period without
// asking go2rtc or the camera.
type AudioStatus struct {
	Camera        AudioStageStatus    `json:"camera"`
	CameraIgnored uint64              `json:"camera_ignored"`
	G711Made      uint64              `json:"g711_made"`
	AACCopy       *AudioStageStatus   `json:"aac_copy,omitempty"`
	AACRunning    bool                `json:"aac_running"`
	Clients       []AudioClientStatus `json:"clients"`
}

func (s *audioStage) status(now time.Time) AudioStageStatus {
	st := AudioStageStatus{Count: s.count, LongestGapS: round1(s.longest(now).Seconds()), Silent: s.silent}
	if !s.last.IsZero() {
		age := round1(now.Sub(s.last).Seconds())
		st.LastAgeS = &age
	}
	return st
}

func round1(f float64) float64 { return float64(int(f*10+0.5)) / 10 }

// audioStatus is the forwarder's audio accounting for /status.
func (rf *RTPForwarder) audioStatus(now time.Time) AudioStatus {
	rf.mutex.RLock()
	defer rf.mutex.RUnlock()
	w := &rf.audio
	st := AudioStatus{
		Camera:        w.camera.status(now),
		CameraIgnored: w.ignored,
		G711Made:      w.made,
		AACRunning:    rf.aac != nil,
		Clients:       []AudioClientStatus{},
	}
	if rf.AACFFmpeg != "" {
		aac := w.aacOut.status(now)
		st.AACCopy = &aac
	}
	ids := make([]string, 0, len(rf.clients))
	for id := range rf.clients {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	for _, id := range ids {
		c := rf.clients[id]
		cs := AudioClientStatus{Session: shortSession(id)}
		if c.wantsG711() {
			g := c.g711.status(now)
			cs.G711 = &g
		}
		if c.aacOn {
			a := c.aac.status(now)
			cs.AAC = &a
		}
		st.Clients = append(st.Clients, cs)
	}
	return st
}
