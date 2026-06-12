package sse

import (
	"bytes"
	"context"
	"encoding/json"
	"io"
	"net/http"
	"os"
	"path/filepath"
	"time"
)

// Default tailing cadence. The Run is polled roughly once a second for new log
// lines; after two consecutive idle polls with the Run no longer active, the
// hub emits the synthetic terminal frame and ends the stream (~2s idle window).
const (
	defaultPollInterval   = 1 * time.Second
	defaultIdleTicksLimit = 2
)

// terminalFrameFor builds the single synthetic completion frame emitted once
// the Run is inactive and the log is idle (Requirement 1.7). The default status
// is "complete"; the reconciled status (Requirement 3) is substituted when a
// resolver is supplied to Stream, so the client renders exactly one of the
// three terminal states (Requirement 3.2).
func terminalFrameFor(status string) string {
	if status == "" {
		status = "complete"
	}
	return "event: pipeline_complete\ndata: {\"status\":\"" + status + "\"}\n\n"
}

// Hub tails agent-log.jsonl and streams events as Server-Sent Events. The zero
// value is usable and applies production defaults; tests may set PollInterval
// and IdleTicksLimit to small values to run fast.
type Hub struct {
	// PollInterval is how often the log file is polled for new lines. Zero uses
	// the production default of ~1s.
	PollInterval time.Duration
	// IdleTicksLimit is how many consecutive idle polls (while the Run is not
	// active) are tolerated before the synthetic terminal frame is emitted.
	// Zero uses the production default of 2 (~2s).
	IdleTicksLimit int
}

// New constructs a Hub with production defaults.
func New() *Hub { return &Hub{} }

// Stream tails logPath and writes SSE frames to w until the client disconnects
// (ctx cancelled) or the Run reaches its terminal condition (no longer active
// and the log idle). It replays from offset 0 on connect so a refresh rebuilds
// the full timeline, and deduplicates by LogEvent.DedupKey so replayed events
// never double-render. If logPath does not exist it is created empty before
// tailing so the stream never errors on a not-yet-started Run.
//
// An optional terminalStatus resolver supplies the reconciled terminal status
// (Requirement 3): when the synthetic terminal frame is emitted it is consulted
// so the frame carries "complete", "escalated", or "error". A nil resolver, or
// one returning "", keeps the default "complete" frame.
func (h *Hub) Stream(ctx context.Context, w http.ResponseWriter, logPath string, isActive func() bool, terminalStatus ...func() string) error {
	poll := h.PollInterval
	if poll <= 0 {
		poll = defaultPollInterval
	}
	idleLimit := h.IdleTicksLimit
	if idleLimit <= 0 {
		idleLimit = defaultIdleTicksLimit
	}

	// Requirement 1.9: ensure the log file exists before tailing.
	if err := ensureFile(logPath); err != nil {
		return err
	}

	// Requirement 1.3: SSE response headers.
	header := w.Header()
	header.Set("Content-Type", "text/event-stream")
	header.Set("Cache-Control", "no-cache")
	header.Set("X-Accel-Buffering", "no")
	w.WriteHeader(http.StatusOK)

	flusher, _ := w.(http.Flusher)
	flush := func() {
		if flusher != nil {
			flusher.Flush()
		}
	}
	flush() // commit headers immediately so the client sees the stream open

	seen := make(map[string]struct{}) // dedup keys already emitted this stream
	tail := &tailer{path: logPath}    // replays from offset 0
	idleTicks := 0

	for {
		// Requirement 1.8: clean close on client disconnect.
		select {
		case <-ctx.Done():
			return nil
		default:
		}

		lines, err := tail.poll()
		if err != nil {
			return err
		}

		if len(lines) == 0 {
			if !isActive() {
				idleTicks++
				if idleTicks >= idleLimit {
					// Requirement 1.7 / 3.2: exactly one synthetic terminal
					// frame, carrying the reconciled terminal status when a
					// resolver is supplied (Requirement 3).
					status := ""
					if len(terminalStatus) > 0 && terminalStatus[0] != nil {
						status = terminalStatus[0]()
					}
					_, _ = io.WriteString(w, terminalFrameFor(status))
					flush()
					return nil
				}
			}
			select {
			case <-ctx.Done():
				return nil
			case <-time.After(poll):
			}
			continue
		}

		idleTicks = 0
		for _, line := range lines {
			if len(bytes.TrimSpace(line)) == 0 {
				continue
			}
			key := dedupKey(line)
			if _, ok := seen[key]; ok {
				continue // Requirement 1.1: suppress duplicate render
			}
			seen[key] = struct{}{}
			// Requirement 1.4 / 1.6: emit the raw JSON line as one SSE frame,
			// including unknown event_type values (never dropped).
			_, _ = io.WriteString(w, "data: "+string(line)+"\n\n")
		}
		flush()
	}
}

// dedupKey parses a raw log line and returns its DedupKey. If the line is not
// valid JSON it falls back to the raw bytes as the identity, so a malformed
// line is still emitted once and never dropped.
func dedupKey(line []byte) string {
	var e LogEvent
	if err := json.Unmarshal(line, &e); err != nil {
		return string(line)
	}
	return e.DedupKey()
}

// ensureFile creates logPath (and any missing parent directories) as an empty
// file if it does not already exist.
func ensureFile(logPath string) error {
	if _, err := os.Stat(logPath); err == nil {
		return nil
	} else if !os.IsNotExist(err) {
		return err
	}
	if err := os.MkdirAll(filepath.Dir(logPath), 0o755); err != nil {
		return err
	}
	f, err := os.OpenFile(logPath, os.O_CREATE|os.O_WRONLY, 0o644)
	if err != nil {
		return err
	}
	return f.Close()
}

// tailer reads complete newline-terminated lines from a file, advancing a byte
// offset across polls and buffering any trailing partial line until it is
// completed. Replaying always starts at offset 0.
type tailer struct {
	path    string
	offset  int64
	partial []byte
}

// poll returns any complete lines written since the last poll.
func (t *tailer) poll() ([][]byte, error) {
	f, err := os.Open(t.path)
	if err != nil {
		return nil, err
	}
	defer f.Close()

	if _, err := f.Seek(t.offset, io.SeekStart); err != nil {
		return nil, err
	}
	data, err := io.ReadAll(f)
	if err != nil {
		return nil, err
	}
	t.offset += int64(len(data))

	buf := append(t.partial, data...)
	var lines [][]byte
	for {
		i := bytes.IndexByte(buf, '\n')
		if i < 0 {
			break
		}
		line := make([]byte, i)
		copy(line, buf[:i])
		lines = append(lines, line)
		buf = buf[i+1:]
	}
	t.partial = append([]byte(nil), buf...)
	return lines, nil
}
