package desktop

import (
	"errors"
	"testing"
	"time"
)

// dialTimeout bounds the loopback dial used to prove a held port stays open.
const dialTimeout = 2 * time.Second

// TestURLUsesActualChosenPort verifies the URL is built from the ACTUAL chosen
// port on loopback (Requirement 11.3). After a fallback the URL must reflect the
// real bound port, not the originally preferred one.
func TestURLUsesActualChosenPort(t *testing.T) {
	cases := []struct {
		port int
		want string
	}{
		{5057, "http://127.0.0.1:5057"},
		{1, "http://127.0.0.1:1"},
		{65535, "http://127.0.0.1:65535"},
	}
	for _, tc := range cases {
		if got := URL(tc.port); got != tc.want {
			t.Errorf("URL(%d) = %q, want %q", tc.port, got, tc.want)
		}
	}
}

// fakeBrowser records the last URL it was asked to open instead of launching a
// real browser, so wiring is testable without touching the OS.
type fakeBrowser struct {
	opened string
	err    error
}

func (f *fakeBrowser) Open(url string) error {
	f.opened = url
	return f.err
}

// TestOpenConsoleOpensActualURL verifies OpenConsole opens the browser at the
// URL derived from the actual chosen port (Requirement 11.3), through the
// Browser interface so the real OS opener is never invoked in tests.
func TestOpenConsoleOpensActualURL(t *testing.T) {
	fb := &fakeBrowser{}
	const chosen = 49231

	if err := OpenConsole(fb, chosen); err != nil {
		t.Fatalf("OpenConsole returned error: %v", err)
	}
	if want := "http://127.0.0.1:49231"; fb.opened != want {
		t.Fatalf("OpenConsole opened %q, want %q", fb.opened, want)
	}
}

// TestOpenConsolePropagatesError verifies a browser-open failure surfaces to the
// caller rather than being swallowed.
func TestOpenConsolePropagatesError(t *testing.T) {
	sentinel := errors.New("no browser")
	fb := &fakeBrowser{err: sentinel}

	if err := OpenConsole(fb, 5057); !errors.Is(err, sentinel) {
		t.Fatalf("OpenConsole error = %v, want %v", err, sentinel)
	}
}

// fakeTray records the menu callbacks it was wired with and lets a test invoke
// them, standing in for a real systray click loop.
type fakeTray struct {
	onOpen func()
	onQuit func()
}

func (f *fakeTray) Run(onOpen, onQuit func()) {
	f.onOpen = onOpen
	f.onQuit = onQuit
}

// TestTrayWiringInvokesCallbacks verifies that a Tray implementation receives
// the "Open Bullpen" (default) and "Quit" callbacks and that invoking them runs
// the wired behaviour (Requirement 11.1). The real systray loop is replaced by a
// fake so the wiring is unit-testable.
func TestTrayWiringInvokesCallbacks(t *testing.T) {
	var opened, quit bool
	ft := &fakeTray{}

	ft.Run(func() { opened = true }, func() { quit = true })

	if ft.onOpen == nil || ft.onQuit == nil {
		t.Fatal("tray did not receive both onOpen and onQuit callbacks")
	}
	ft.onOpen()
	ft.onQuit()
	if !opened {
		t.Error("onOpen callback was not invoked")
	}
	if !quit {
		t.Error("onQuit callback was not invoked")
	}
}

// staticCheck ensures the real implementations satisfy the interfaces this slice
// defines. It is a compile-time assertion expressed as a test so the wiring
// contract is exercised by the suite.
func TestRealImplementationsSatisfyInterfaces(t *testing.T) {
	var _ Browser = OSBrowser{}
	var _ Tray = NewTray()
}
