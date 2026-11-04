package rtsp

import (
	"context"
	"testing"
	"time"

	"avent-webrtc-bridge/pkg/lan"

	tuyalan "github.com/luukvisser/tuya-lan"
)

// sessionLANClient is a fakeLANClient with the tuya-lan session the share
// hub is handed; that session is never started.
type sessionLANClient struct {
	*fakeLANClient
	session *tuyalan.Client
}

func (c *sessionLANClient) Session() *tuyalan.Client { return c.session }

func sharedLANSource(t *testing.T, hub *ShareHub) (*LANSource, *fakeLANClient) {
	t.Helper()
	fake := &fakeLANClient{start: func(f *fakeLANClient) error {
		go f.cfg.OnVideo(testIDR)
		return nil
	}}
	src, _ := newTestLANSource(t, fake)
	src.share = hub
	session := tuyalan.NewClient("dev1", "lk", "pw", "uid1", nil, nil, nil, nil)
	src.newClient = func(cfg lan.ClientConfig) lanClient {
		fake.cfg = cfg
		return &sessionLANClient{fake, session}
	}
	ctx, cancel := context.WithTimeout(context.Background(), time.Second)
	defer cancel()
	if err := src.Start(ctx); err != nil {
		t.Fatalf("Start: %v", err)
	}
	return src, fake
}

func TestLANSourceSharesItsSession(t *testing.T) {
	hub := &ShareHub{}
	src, fake := sharedLANSource(t, hub)
	if c := hub.counters("dev1"); c.Sessions != 1 {
		t.Fatalf("sessions = %d, want the source's session attached", c.Sessions)
	}
	if fake.cfg.OnDPS == nil {
		t.Fatal("the source does not pass the monitor's pushes on")
	}
	fake.cfg.OnDPS(map[string]any{"212": "record"}, time.Now())
	if c := hub.counters("dev1"); c.Pushes != 1 {
		t.Fatalf("pushes = %d, want 1", c.Pushes)
	}
	src.Stop()
	if fake.closed.Load() != 1 {
		t.Fatal("without a subscriber Stop must close the session at once")
	}
}

func TestLANSourceKeepsTheSessionForASubscriber(t *testing.T) {
	hub := &ShareHub{}
	src, fake := sharedLANSource(t, hub)
	sub, _, err := hub.subscribe("dev1")
	if err != nil {
		t.Fatalf("subscribe: %v", err)
	}
	src.Stop()
	if fake.closed.Load() != 0 || fake.ctx.Err() != nil {
		t.Fatal("the session closed while the integration was still subscribed")
	}
	hub.unsubscribe(sub)
	select {
	case <-fake.done:
	case <-time.After(time.Second):
		t.Fatal("the session stayed open after the subscriber left")
	}
}
