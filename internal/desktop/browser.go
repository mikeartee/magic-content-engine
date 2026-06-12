package desktop

import (
	"os/exec"
	"runtime"
)

// Browser opens a URL in the user's default browser. It is an interface so the
// wiring (build the chosen URL, open it) is unit-testable with a fake and the
// real OS opener is never invoked during tests (Requirement 11.3).
type Browser interface {
	Open(url string) error
}

// OSBrowser is the real Browser: it launches the platform default browser via
// the OS opener. On Windows it uses rundll32 url.dll,FileProtocolHandler — the
// standard, shell-free way to hand a URL to the default browser.
type OSBrowser struct{}

// Open launches the default browser at url. The child process is started and
// not waited on, so the caller is not blocked by the browser lifetime.
func (OSBrowser) Open(url string) error {
	switch runtime.GOOS {
	case "windows":
		return exec.Command("rundll32", "url.dll,FileProtocolHandler", url).Start()
	case "darwin":
		return exec.Command("open", url).Start()
	default:
		return exec.Command("xdg-open", url).Start()
	}
}

// OpenConsole opens the browser at the Console URL for the actual chosen port
// (Requirement 11.3). It centralises URL construction so callers cannot open a
// stale, pre-fallback port.
func OpenConsole(b Browser, port int) error {
	return b.Open(URL(port))
}
