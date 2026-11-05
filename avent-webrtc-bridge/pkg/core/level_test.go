package core

import (
	"testing"

	"github.com/rs/zerolog"
)

func TestDefaultLevelIsInfo(t *testing.T) {
	t.Setenv("LOG_LEVEL", "")
	InitLogger()
	if got := Logger.GetLevel(); got != zerolog.InfoLevel {
		t.Fatalf("default level %s, want info", got)
	}
	if Logger.Debug().Enabled() {
		t.Fatal("debug lines are written by default")
	}
}

func TestSetLevel(t *testing.T) {
	t.Setenv("LOG_LEVEL", "")
	InitLogger()
	if !SetLevel("debug") || Logger.GetLevel() != zerolog.DebugLevel {
		t.Fatalf("SetLevel(debug) left %s", Logger.GetLevel())
	}
	if SetLevel("loud") || Logger.GetLevel() != zerolog.DebugLevel {
		t.Fatalf("an unknown name changed the level to %s", Logger.GetLevel())
	}
	if SetLevel("") {
		t.Fatal("an empty name reported a change")
	}
}

func TestEnvironmentWins(t *testing.T) {
	t.Setenv("LOG_LEVEL", "warn")
	InitLogger()
	if Logger.GetLevel() != zerolog.WarnLevel {
		t.Fatalf("LOG_LEVEL=warn gave %s", Logger.GetLevel())
	}
	SetLevel("trace")
	if Logger.GetLevel() != zerolog.WarnLevel {
		t.Fatalf("the config overrode LOG_LEVEL: %s", Logger.GetLevel())
	}
}

func TestMaskID(t *testing.T) {
	for in, want := range map[string]string{
		"":                       "",
		"abc":                    "abc",
		"abcdef":                 "abcdef",
		"a1b2c3d4e5f0e1d2c3b4a5": "a1b2c3…",
	} {
		if got := MaskID(in); got != want {
			t.Errorf("MaskID(%q) = %q, want %q", in, got, want)
		}
	}
}
