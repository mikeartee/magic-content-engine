// Package sse implements the Bullpen Console's Server-Sent Events hub: it tails
// a Run's agent-log.jsonl, replays the full timeline from offset 0 on every
// connect, deduplicates replayed events so a browser refresh or EventSource
// reconnect never double-renders, and emits one synthetic terminal frame when
// the Run is finished. This implements Requirement 1 of the bullpen-console-go
// spec and the "SSE Contract" / "SSE Hub" sections of the design.
package sse

// LogEvent is one line of agent-log.jsonl, mirroring the Python AMILogEvent the
// pipeline writes. Unknown event_type values are still valid LogEvents and are
// emitted raw rather than dropped (Requirement 1.6).
type LogEvent struct {
	EventType string         `json:"event_type"`
	Timestamp string         `json:"timestamp"` // ISO 8601
	AgentType string         `json:"agent_type"`
	RunID     string         `json:"run_id"`
	Details   map[string]any `json:"details"`
}

// DedupKey is the composite identity used to suppress duplicate rendering of
// replayed events: timestamp | event_type | agent_type (Requirement 1.1). Both
// the server and the client key their dedup sets on this exact string so that
// even if offsets disagree across a reconnect, each event renders once.
func (e LogEvent) DedupKey() string {
	return e.Timestamp + "|" + e.EventType + "|" + e.AgentType
}
