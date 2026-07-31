package api

import (
	"encoding/json"
	"testing"
)

// Regression: the real /api/v1/memories response's "source" field is an
// OBJECT ({"type": "...", "project": "...", ...}), not a string -- the Memory
// struct used to declare it as `Source string`, so every single memory
// list/search call failed to decode. The error was swallowed silently (errMsg
// was only rendered on screenChat), so the Memory Browser just looked
// permanently empty with zero indication anything was wrong. Confirmed
// against a real captured payload before fixing.
func TestMemory_DecodesRealAPIPayloadWithObjectSource(t *testing.T) {
	raw := `[{"id":"6a6c0a278387d543d2012c1d","content":"example memory","content_type":"preference",` +
		`"categories":["claude_session","decision"],"importance":0.7,"confidence":null,"verified":false,` +
		`"created_at":"2026-07-31T02:36:23.072000Z",` +
		`"source":{"type":"claude_session_digest","project":"~/Development/x","digested_at":"2026-07-31T02:36:23.036000Z"},` +
		`"access_count":0}]`

	var memories []Memory
	if err := json.Unmarshal([]byte(raw), &memories); err != nil {
		t.Fatalf("Memory failed to decode a real object-shaped 'source' field: %v", err)
	}
	if len(memories) != 1 || memories[0].Content != "example memory" {
		t.Fatalf("unexpected decode result: %+v", memories)
	}
}
