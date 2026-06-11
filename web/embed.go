// Package web embeds the Bullpen Console UI assets into the binary so the
// Console is self-contained and has no asset-path fragility (single-machine
// scope). Assets are served by internal/server via embed.FS.
package web

import (
	"embed"
	"io/fs"
)

//go:embed static
var staticFiles embed.FS

// Static returns the embedded UI file system rooted at the static directory,
// so that "index.html" and sibling assets are addressable at the top level.
func Static() fs.FS {
	sub, err := fs.Sub(staticFiles, "static")
	if err != nil {
		// The embed directive guarantees the static directory exists at build
		// time, so this can only fail on a programmer error.
		panic(err)
	}
	return sub
}
