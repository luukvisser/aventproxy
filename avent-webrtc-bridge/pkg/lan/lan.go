// Package lan streams from a monitor over the local network: the client the
// LAN and imm sources drive (lan_source.go, imm_source.go). The protocol is
// github.com/luukvisser/tuya-lan, a module made from the same code (Leonard
// Pitzu's pkg/lan) with the hardening and the cloud route of this package's
// earlier version folded in, plus Tuya LAN protocols 3.4 and 3.3, 1080p and
// data points on the streaming session. This file keeps the API the sources
// were written against, so they need no change.
package lan

import (
	"context"
	"errors"
	"fmt"
	"time"

	tuyalan "github.com/luukvisser/tuya-lan"
)

// The protocol's types and frames, as tuya-lan defines them.
type (
	VideoFrame      = tuyalan.VideoFrame
	AudioFrame      = tuyalan.AudioFrame
	SignalFrame     = tuyalan.SignalFrame
	SignalHeader    = tuyalan.SignalHeader
	SignalMessage   = tuyalan.SignalMessage
	ICEServer       = tuyalan.ICEServer
	Signaller       = tuyalan.Signaller
	DisconnectFrame = tuyalan.DisconnectFrame
	Version         = tuyalan.Version
)

const (
	TypeOffer      = tuyalan.TypeOffer
	TypeAnswer     = tuyalan.TypeAnswer
	TypeCandidate  = tuyalan.TypeCandidate
	TypeDisconnect = tuyalan.TypeDisconnect
	PathLAN        = tuyalan.PathLAN
	PathMQTT       = tuyalan.PathMQTT
)

// ParseSignalFrame reads the parts of a 302 frame a session acts on.
var ParseSignalFrame = tuyalan.ParseSignalFrame

// The steps a start can fail at, and the ways a running stream ends.
var (
	ErrOfferNotSent = tuyalan.ErrOfferNotSent
	ErrRefused      = tuyalan.ErrRefused
	ErrNoAnswer     = tuyalan.ErrNoAnswer
	ErrBadAnswer    = tuyalan.ErrBadAnswer
	ErrNoCandidate  = tuyalan.ErrNoCandidate
	ErrICE          = tuyalan.ErrICE
	ErrLogin        = tuyalan.ErrLogin
	// ErrEnded is a live session the monitor ended with a disconnect.
	ErrEnded = tuyalan.ErrCameraEnded
	// ErrMediaIdle is a stream on which no conversation delivered a media
	// message for mediaIdleTimeout.
	ErrMediaIdle = tuyalan.ErrMediaIdle
)

const (
	// startTimeout caps the handshake when the caller's context does not.
	startTimeout = 20 * time.Second
	// mediaIdleTimeout is how long every media conversation may stay silent
	// before the stream counts as dead; audio alone arrives about 50 times a
	// second.
	mediaIdleTimeout = 3 * time.Second
)

// ClientConfig names the monitor and where its media goes. Any callback may
// be nil, in which case that stream is discarded. Callbacks run on the
// client's own goroutines.
type ClientConfig struct {
	DeviceID string
	LocalKey string
	Password string
	UID      string
	// IP is the monitor's address, and Hosts more of them to try (the
	// handshake proves the key, so a list shared by several monitors is
	// fine). The integration discovers them; the bridge never does.
	IP    string
	Hosts []string
	// Protocol is the Tuya LAN protocol the integration's session negotiated,
	// such as "3.4", and the only one tried; empty tries 3.5, then 3.4.
	Protocol string
	// Clarity is the video the startup sequence asks for: tuyalan.ClarityHD
	// (1920x1080) or tuyalan.ClarityStandard (1280x720, what the app asks
	// for). Zero means ClarityStandard.
	Clarity uint32
	// StartupGap bounds the pause between two startup messages; zero means
	// tuyalan.DefaultStartupGap.
	StartupGap time.Duration

	OnVideo func(*VideoFrame)
	OnAudio func(*AudioFrame)
	// OnClosed reports, once, a stream that died while open: the monitor
	// ended it, no conversation delivered media for mediaIdleTimeout, or the
	// signalling session failed. It is not called after Close or after the
	// Start context ended.
	OnClosed func(error)
	// OnDPS receives the data points the monitor pushes on a LAN session.
	OnDPS func(dps map[string]any, at time.Time)

	// Signaller, when set, carries the session's 302 frames instead of the
	// 6668 session: the imm route over the cloud broker. IP is then only a
	// hint; MotoID and ICEServers fill the offer.
	Signaller  Signaller
	MotoID     string
	ICEServers []ICEServer
	// HandshakeTimeout caps the handshake in place of startTimeout when set.
	HandshakeTimeout time.Duration
	// OnControl is told the size of everything the control conversation
	// delivers after the login.
	OnControl func(n int)
}

// Client streams from one monitor. It is used once: Start, then Close.
type Client struct {
	cfg ClientConfig
	c   *tuyalan.Client
}

// NewClient prepares a client; nothing touches the network until Start.
func NewClient(cfg ClientConfig) *Client {
	hosts := cfg.Hosts
	if cfg.IP != "" {
		hosts = append([]string{cfg.IP}, hosts...)
	}
	// A configured version is the only one offered: the integration writes the
	// version its own session negotiated and rewrites it when a firmware update
	// changes it, so trying another adds nothing but risk (tuya-lan's 3.5 offer
	// reuses its GCM nonce, signal.go).
	var versions []tuyalan.Version
	if v, err := tuyalan.ParseVersion(cfg.Protocol); err == nil {
		versions = []tuyalan.Version{v}
	}
	c := tuyalan.NewClient(cfg.DeviceID, cfg.LocalKey, cfg.Password, cfg.UID, hosts, versions, cfg.OnVideo, cfg.OnAudio)
	c.Clarity = cfg.Clarity
	c.StartupGap = cfg.StartupGap
	c.OnError = cfg.OnClosed
	c.OnDPS = cfg.OnDPS
	c.Signaller, c.MotoID, c.ICEServers = cfg.Signaller, cfg.MotoID, cfg.ICEServers
	c.OnControl = cfg.OnControl
	c.MediaIdleTimeout = mediaIdleTimeout
	return &Client{cfg: cfg, c: c}
}

// Start negotiates a session and begins streaming.
//
// ctx bounds the handshake (at most startTimeout, or HandshakeTimeout) and is
// also the stream's life: when it ends the client closes as with Close,
// without calling OnClosed. On error everything Start opened is closed.
func (c *Client) Start(ctx context.Context) error {
	if c.cfg.IP == "" && len(c.cfg.Hosts) == 0 && c.cfg.Signaller == nil {
		return fmt.Errorf("lan: no usable address for %s", c.cfg.DeviceID)
	}
	// Closing is also what unblocks a handshake step in progress.
	context.AfterFunc(ctx, func() { _ = c.Close() })
	timeout := startTimeout
	if c.cfg.HandshakeTimeout > 0 {
		timeout = c.cfg.HandshakeTimeout
	}
	setup, done := context.WithTimeout(ctx, timeout)
	defer done()
	if err := c.c.Start(setup); err != nil {
		if errors.Is(err, context.DeadlineExceeded) && ctx.Err() == nil {
			return fmt.Errorf("lan: handshake took over %v: %w", timeout, err)
		}
		return err
	}
	return nil
}

// Close tears the session down. OnClosed is not called after it.
func (c *Client) Close() error { return c.c.Close() }

// Session is the tuya-lan client underneath, for the data points on the
// streaming session (SetDPS, QueryDPS) and the media counters (Snapshot).
func (c *Client) Session() *tuyalan.Client { return c.c }
