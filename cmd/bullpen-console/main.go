// Command bullpen-console is the single binary entry point for the Bullpen
// Console. It selects a loopback port (preferring the configured/default port
// and falling back to an OS-assigned free port without killing any process),
// starts a loopback-only net/http server that serves the embedded UI and the
// API, opens the default browser at the actual chosen URL, and runs a native
// system tray ("Open Bullpen" default + "Quit"). It boots with no AWS
// credentials and no AWS SDK (Requirement 5); AWS stays entirely on the Python
// side. The tray, port, and browser shims implement Requirement 11.
package main

import (
	"context"
	"flag"
	"log"
	"net"
	"net/http"
	"os/exec"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/desktop"
	"github.com/mikeartee/magic-content-engine/console/internal/devto"
	"github.com/mikeartee/magic-content-engine/console/internal/files"
	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
	"github.com/mikeartee/magic-content-engine/console/internal/vault"
	"github.com/mikeartee/magic-content-engine/console/web"
)

// outputRoot is the parent directory of every output/<run_id>/ run bundle.
const outputRoot = "output"

func main() {
	// 0 means "no explicit port configured" so PickPort uses its default; a
	// positive value is treated as the preferred port (Requirement 11.2).
	port := flag.Int("port", 0, "preferred loopback port (0 = default with OS-assigned fallback)")
	flag.Parse()

	// Native port handling: prefer the configured/default port, fall back to an
	// OS-assigned free port if it is taken — without killing any process.
	chosen, err := desktop.PickPort(*port)
	if err != nil {
		log.Fatalf("could not select a port: %v", err)
	}

	rm := run.New(outputRoot, run.DefaultStarter,
		// Observe runner exit so the single-active slot is released and the SSE
		// hub settles into its terminal frame once the pipeline finishes.
		run.WithCompletionWatch(func(c *exec.Cmd) error { return c.Wait() }))

	srv := server.New(web.Static(),
		server.WithOutputDir(outputRoot),
		// Let the SSE hub know whether a given run_id is still streaming, so it
		// holds the connection open during a live Run and only emits the
		// terminal frame after the runner has exited.
		server.WithActiveProbe(func(runID string) bool {
			h, ok := rm.Active()
			return ok && h.RunID == runID
		}))
	srv.SetRunManager(rm)
	srv.SetFileService(files.New(outputRoot))
	srv.SetSuggestionService(vault.New())
	srv.SetDevtoPublisher(devto.New(outputRoot))
	addr := server.ListenAddr(chosen)

	// Bind the chosen port up front so the actual listening address is known
	// before the browser is opened (Requirement 11.3).
	ln, err := net.Listen("tcp", addr)
	if err != nil {
		log.Fatalf("could not bind %s: %v", addr, err)
	}

	httpServer := &http.Server{
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	go func() {
		log.Printf("Bullpen Console listening on %s", desktop.URL(chosen))
		if err := httpServer.Serve(ln); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server error: %v", err)
		}
	}()

	// Open the default browser at the ACTUAL chosen URL (Requirement 11.3).
	if err := desktop.OpenConsole(desktop.OSBrowser{}, chosen); err != nil {
		log.Printf("could not open browser: %v", err)
	}

	// Native tray: "Open Bullpen" (default) reopens the browser; "Quit" shuts the
	// server down cleanly (Requirement 11.1). RunTray blocks on the native loop.
	tray := desktop.NewTray()
	tray.Run(
		func() {
			if err := desktop.OpenConsole(desktop.OSBrowser{}, chosen); err != nil {
				log.Printf("could not open browser: %v", err)
			}
		},
		func() {
			ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
			defer cancel()
			_ = httpServer.Shutdown(ctx)
		},
	)
}
