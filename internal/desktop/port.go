// Package desktop provides the native desktop shims for the Bullpen Console:
// loopback port selection with OS-assigned fallback (PickPort), the chosen-URL
// builder, native browser launch, and the system tray. Together they replace
// pystray and the netstat/taskkill dance in bullpen.bat (Requirement 11).
//
// The port and URL logic is pure and fully unit-tested. The tray and browser
// shims touch the OS, so each sits behind a small interface (Tray, Browser) and
// the real implementations stay as thin as possible.
package desktop

import (
	"net"

	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// DefaultPort is the preferred loopback port used when no port is configured
// (Requirement 11.2). It matches the historical Console default so existing
// bookmarks keep working when the port is free.
const DefaultPort = 5057

// PickPort selects a loopback port to listen on (Requirement 11.2). It prefers
// `preferred`; when `preferred` is not positive it falls back to DefaultPort as
// the preference. It attempts to bind the preferred port on 127.0.0.1 and, if
// that port is already taken, asks the OS for a free port (bind :0) instead.
// It NEVER terminates the process holding the preferred port — a deliberate
// departure from the netstat/taskkill approach in bullpen.bat.
//
// The returned port is known-bindable at the moment of the call; the caller
// re-binds it to serve. On a busy host the usual local-desktop race window is
// acceptable, and a re-bind failure surfaces normally to the caller.
func PickPort(preferred int) (int, error) {
	if preferred <= 0 {
		preferred = DefaultPort
	}

	// Try the preferred port first.
	if ln, err := net.Listen("tcp", server.ListenAddr(preferred)); err == nil {
		_ = ln.Close()
		return preferred, nil
	}

	// Preferred port is taken: let the OS assign a free one. We do not touch the
	// process that holds the preferred port.
	ln, err := net.Listen("tcp", server.ListenAddr(0))
	if err != nil {
		return 0, err
	}
	port := ln.Addr().(*net.TCPAddr).Port
	_ = ln.Close()
	return port, nil
}

// URL builds the loopback Console URL for the given port (Requirement 11.3).
// Callers pass the ACTUAL chosen port (after any PickPort fallback) so the URL
// always reflects the real bound port. It reuses server.ListenAddr so the host
// convention (127.0.0.1) stays in one place.
func URL(port int) string {
	return "http://" + server.ListenAddr(port)
}
