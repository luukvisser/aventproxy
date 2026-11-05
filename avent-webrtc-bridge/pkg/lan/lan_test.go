package lan

import (
	"context"
	"errors"
	"testing"

	tuyalan "github.com/luukvisser/tuya-lan"
)

func TestNewClientMapsTheConfig(t *testing.T) {
	c := NewClient(ClientConfig{
		DeviceID: "dev", LocalKey: "test-local-key-0", Password: "pw", UID: "uid",
		IP: "10.0.0.7", Hosts: []string{"10.0.0.8"}, Protocol: "3.4", Clarity: tuyalan.ClarityHD,
	})
	s := c.Session()
	if len(s.Hosts) != 2 || s.Hosts[0] != "10.0.0.7" || s.Hosts[1] != "10.0.0.8" {
		t.Errorf("hosts = %v, want the address first, then the others", s.Hosts)
	}
	// A configured version is the only one offered (no 3.5 second, whose
	// offer reuses its GCM nonce).
	if len(s.Versions) != 1 || s.Versions[0] != tuyalan.V34 {
		t.Errorf("versions = %v, want 3.4 only", s.Versions)
	}
	if s.Clarity != tuyalan.ClarityHD || s.MediaIdleTimeout != mediaIdleTimeout {
		t.Errorf("clarity %d, idle %v", s.Clarity, s.MediaIdleTimeout)
	}
}

// 3.3 is tried alone: it proves no key in its handshake.
func TestProtocol33IsTriedAlone(t *testing.T) {
	s := NewClient(ClientConfig{DeviceID: "dev", IP: "10.0.0.7", Protocol: "3.3"}).Session()
	if len(s.Versions) != 1 || s.Versions[0] != tuyalan.V33 {
		t.Errorf("versions = %v, want 3.3 only", s.Versions)
	}
}

func TestStartRefusesWithoutAnAddress(t *testing.T) {
	err := NewClient(ClientConfig{DeviceID: "dev"}).Start(context.Background())
	if err == nil {
		t.Fatal("started without an address or a signaller")
	}
}

func TestTheErrorsAreTuyaLANs(t *testing.T) {
	if !errors.Is(ErrEnded, tuyalan.ErrCameraEnded) || !errors.Is(ErrMediaIdle, tuyalan.ErrMediaIdle) {
		t.Fatal("the adapter's errors must be the module's, for errors.Is")
	}
}
