// Command bullpen-console is the single binary entry point for the Bullpen
// Console. For this skeleton slice it starts a loopback-only net/http server
// that serves the embedded UI and exposes GET /api/health. It boots with no
// AWS credentials and no AWS SDK (Requirement 5); AWS stays entirely on the
// Python side.
//
// Later slices add the run manager, SSE hub, file service, vault suggestions,
// dev.to publisher, and the native system tray + port picker + browser launch.
package main

import (
	"flag"
	"log"
	"net/http"
	"time"

	"github.com/mikeartee/magic-content-engine/console/internal/files"
	"github.com/mikeartee/magic-content-engine/console/internal/run"
	"github.com/mikeartee/magic-content-engine/console/internal/server"
	"github.com/mikeartee/magic-content-engine/console/web"
)

// defaultPort is the preferred loopback port used when none is configured. The
// full preferred-then-OS-assigned fallback (PickPort) arrives in slice #46;
// this skeleton keeps the default in one place so it does not block that work.
const defaultPort = 5057

// outputRoot is the parent directory of every output/<run_id>/ run bundle.
const outputRoot = "output"

func main() {
	port := flag.Int("port", defaultPort, "loopback port to listen on")
	flag.Parse()

	srv := server.New(web.Static(), server.WithOutputDir(outputRoot))
	srv.SetRunManager(run.New(outputRoot, run.DefaultStarter))
	srv.SetFileService(files.New(outputRoot))
	addr := server.ListenAddr(*port)

	httpServer := &http.Server{
		Addr:              addr,
		Handler:           srv.Routes(),
		ReadHeaderTimeout: 5 * time.Second,
	}

	log.Printf("Bullpen Console listening on http://%s", addr)
	if err := httpServer.ListenAndServe(); err != nil && err != http.ErrServerClosed {
		log.Fatalf("server error: %v", err)
	}
}
