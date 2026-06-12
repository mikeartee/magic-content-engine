package desktop

import "fyne.io/systray"

// Tray runs a system tray with an "Open Bullpen" default action and a "Quit"
// action (Requirement 11.1). It is an interface so the wiring is testable with a
// fake; the real implementation drives a native systray event loop, which is not
// itself unit-testable.
//
// Run blocks for the lifetime of the tray (the native main loop). onOpen is
// invoked when the user picks "Open Bullpen"; onQuit is invoked once when the
// tray is shutting down so the caller can stop the server.
type Tray interface {
	Run(onOpen, onQuit func())
}

// NewTray returns the real, native system tray implementation backed by
// fyne.io/systray.
func NewTray() Tray {
	return systrayTray{}
}

// systrayTray is the native Tray backed by fyne.io/systray. It is a thin shim:
// it builds the menu, treats "Open Bullpen" as the default action, and forwards
// "Quit" to systray.Quit so the onExit hook fires.
type systrayTray struct{}

// Run starts the native tray loop and blocks until the tray exits. The menu has
// "Open Bullpen" first (the default item) and "Quit" last (Requirement 11.1).
func (systrayTray) Run(onOpen, onQuit func()) {
	onReady := func() {
		systray.SetTitle("Bullpen")
		systray.SetTooltip("Bullpen Console")

		mOpen := systray.AddMenuItem("Open Bullpen", "Open the Bullpen Console")
		systray.AddSeparator()
		mQuit := systray.AddMenuItem("Quit", "Quit the Bullpen Console")

		go func() {
			for {
				select {
				case <-mOpen.ClickedCh:
					if onOpen != nil {
						onOpen()
					}
				case <-mQuit.ClickedCh:
					systray.Quit()
					return
				}
			}
		}()
	}

	onExit := func() {
		if onQuit != nil {
			onQuit()
		}
	}

	systray.Run(onReady, onExit)
}
