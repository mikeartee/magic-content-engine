package desktop

import (
	"net"
	"testing"

	"github.com/mikeartee/magic-content-engine/console/internal/server"
)

// TestPickPortPrefersFreePreferred verifies that when the preferred port is
// free, PickPort returns exactly that port (Requirement 11.2).
func TestPickPortPrefersFreePreferred(t *testing.T) {
	preferred := grabFreePort(t)

	got, err := PickPort(preferred)
	if err != nil {
		t.Fatalf("PickPort(%d) returned error: %v", preferred, err)
	}
	if got != preferred {
		t.Fatalf("PickPort(%d) = %d, want the preferred port %d", preferred, got, preferred)
	}
	// The returned port must be bindable on loopback.
	assertBindable(t, got)
}

// TestPickPortNoPreferredUsesDefault verifies that when no port is configured
// (preferred <= 0), PickPort uses the package DefaultPort when it is free
// (Requirement 11.2: "start listening using a default port").
func TestPickPortNoPreferredUsesDefault(t *testing.T) {
	// Only assert the default is chosen when it is actually free; otherwise the
	// fallback path (covered elsewhere) legitimately picks another port.
	ln, err := net.Listen("tcp", server.ListenAddr(DefaultPort))
	if err != nil {
		t.Skipf("default port %d not free on this host; skipping", DefaultPort)
	}
	_ = ln.Close()

	got, err := PickPort(0)
	if err != nil {
		t.Fatalf("PickPort(0) returned error: %v", err)
	}
	if got != DefaultPort {
		t.Fatalf("PickPort(0) = %d, want DefaultPort %d", got, DefaultPort)
	}
}

// TestPickPortFallsBackWhenPreferredTaken verifies the heart of this slice:
// when the preferred port is already bound, PickPort returns a DIFFERENT free
// port without error and WITHOUT terminating the holder of the preferred port
// (Requirement 11.2: no process killing).
func TestPickPortFallsBackWhenPreferredTaken(t *testing.T) {
	// Occupy a port and hold it open for the duration of the test. This stands
	// in for "another process already bound the preferred port".
	holder, err := net.Listen("tcp", server.ListenAddr(0))
	if err != nil {
		t.Fatalf("failed to occupy a port: %v", err)
	}
	defer holder.Close()
	taken := holder.Addr().(*net.TCPAddr).Port

	got, err := PickPort(taken)
	if err != nil {
		t.Fatalf("PickPort(%d) returned error: %v", taken, err)
	}
	if got == taken {
		t.Fatalf("PickPort(%d) = %d, want a DIFFERENT free port", taken, got)
	}

	// The holder must still be listening: no process/listener was killed.
	if _, err := net.DialTimeout("tcp", server.ListenAddr(taken), dialTimeout); err != nil {
		t.Fatalf("holder of preferred port %d is no longer listening (it was killed): %v", taken, err)
	}

	// The fallback port must itself be bindable.
	assertBindable(t, got)
}

// grabFreePort binds :0, records the port, then releases it so it is free for
// the caller to use as a "preferred" port.
func grabFreePort(t *testing.T) int {
	t.Helper()
	ln, err := net.Listen("tcp", server.ListenAddr(0))
	if err != nil {
		t.Fatalf("failed to grab a free port: %v", err)
	}
	port := ln.Addr().(*net.TCPAddr).Port
	if err := ln.Close(); err != nil {
		t.Fatalf("failed to release grabbed port: %v", err)
	}
	return port
}

// assertBindable confirms the given port can be bound on loopback right now.
func assertBindable(t *testing.T, port int) {
	t.Helper()
	ln, err := net.Listen("tcp", server.ListenAddr(port))
	if err != nil {
		t.Fatalf("returned port %d is not bindable: %v", port, err)
	}
	_ = ln.Close()
}
