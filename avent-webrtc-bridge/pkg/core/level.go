package core

import (
	"os"
	"strings"

	"github.com/rs/zerolog"
)

// DefaultLevel is info. Trace and debug carry the RTSP exchanges, device ids
// and LAN addresses, which do not belong in a log a user pastes into an issue;
// the bridge used to log at trace unconditionally.
const DefaultLevel = zerolog.InfoLevel

func parseLevel(name string) (zerolog.Level, bool) {
	name = strings.ToLower(strings.TrimSpace(name))
	if name == "" {
		return zerolog.NoLevel, false
	}
	lvl, err := zerolog.ParseLevel(name)
	if err != nil || lvl == zerolog.NoLevel {
		return zerolog.NoLevel, false
	}
	return lvl, true
}

// SetLevel sets the log level from a name: trace, debug, info, warn or error.
// LOG_LEVEL in the environment wins over it. An empty or unknown name keeps
// the current level and reports false.
func SetLevel(name string) bool {
	if _, ok := parseLevel(os.Getenv("LOG_LEVEL")); ok {
		return true
	}
	lvl, ok := parseLevel(name)
	if ok {
		Logger = Logger.Level(lvl)
	}
	return ok
}

// MaskID shortens a Tuya device id for a log line to its first six characters
// and an ellipsis: enough to tell a household's cameras apart (a path
// collision suffix uses the same six), not enough to identify the device.
func MaskID(id string) string {
	if len(id) <= 6 {
		return id
	}
	return id[:6] + "…"
}
