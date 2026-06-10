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

	"github.com/mikeartee/magic-content-engine/console/internal/server"
	"github.com/mikeartee/magic-content-engine/console/web"
)

func main() {
	port := flag.Int("port", 8765, "loopback port to listen on")
	flag.Parse()

	srv := server.New(web.Static())
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
