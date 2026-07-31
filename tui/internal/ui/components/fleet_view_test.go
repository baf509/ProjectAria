package components

import (
	"strings"
	"testing"
	"time"

	"github.com/ben/aria-tui/internal/api"
)

// Regression: lipgloss.Style.Render(text + "\n") -- baking the trailing
// newline INTO the styled string instead of appending it after Render()
// returns -- desyncs a bubbles Viewport's line-based content by one line,
// silently swallowing whatever content line immediately follows. This ate
// the first session row in the Fleet table (index 0, sessions come back
// newest-first) on a real 200-column terminal with a real 8-session fleet,
// discovered live when a just-started Ridge pi-code session never appeared
// in Fleet despite the header correctly counting it in "(8 sessions, ...)".
// Reproducing this synthetically turned out to be sensitive to the exact
// cumulative content length, so this test mirrors the real fleet composition
// (mixed backends, a completed "pool" session, realistic workspace paths)
// rather than a minimal synthetic case.
func TestFleetView_AllSessionRowsSurviveAtWideWidth(t *testing.T) {
	fv := NewFleetView()
	fv.SetSize(200, 44) // matches the terminal size (200x50) that showed the bug live
	now := time.Now()
	sessions := []api.CodingSession{
		{ID: "46816e51-a697-4494-bb82-895f5f233a05", Backend: "pi-code", Model: "qwen3.6-35b-a3b", LLM: "ridge",
			Workspace: "/home/ben/Development/war-audio-game/.worktrees/ridge_review-20260731-022930-423536",
			Status:    "running", CreatedAt: now.Add(-27 * time.Minute)},
		{ID: "0c3b657f-4951-48a1-ac04-76a630bd4e59", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/aria-projects", Status: "running", CreatedAt: now.Add(-59 * time.Minute)},
		{ID: "0efcc9a7-0019-4781-a521-34f5b818b22e", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/aria-projects", Status: "running", CreatedAt: now.Add(-time.Hour)},
		{ID: "b432f986-c06f-435d-9778-48e737b409ac", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/ProjectAria", Status: "stopped", CreatedAt: now.Add(-22 * time.Hour)},
		{ID: "4ce8dfbe-eed9-456a-ac85-138693a8c9ed", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/ProjectAria", Status: "stopped", CreatedAt: now.Add(-22 * time.Hour)},
		{ID: "4c78077a-e04f-4368-8340-d33a065a0a5e", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/ProjectAria", Status: "stopped", CreatedAt: now.Add(-22 * time.Hour)},
		{ID: "e824ad52-0630-447b-b96f-d74d75a355f7", Backend: "claude_code", Model: "claude",
			Workspace: "/home/ben/Development/ProjectAria", Status: "stopped", CreatedAt: now.Add(-22 * time.Hour)},
		{ID: "49793f2b-02e5-4732-b184-df8051e5e199", Backend: "pool",
			Workspace: "/home/ben/Development/ProjectAria", Status: "completed", CreatedAt: now.Add(-48 * time.Hour)},
	}
	shells := []api.Shell{
		{Name: "claude-ProjectAria", ShortName: "ProjectAria", Host: "corsair-ai", Status: "active", ActivityState: "working"},
		{Name: "claude-emuDeviceConfig", ShortName: "emuDeviceConfig", Host: "corsair-ai", Status: "active", ActivityState: "working"},
	}
	fv.SetData(sessions, shells, nil)
	content := fv.Viewport.View()

	// Workspaces repeat across sessions, so a substring check can't confirm
	// every ROW survived -- count session rows directly instead.
	rowCount := 0
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "▸"))
		if strings.HasPrefix(strings.TrimSpace(trimmed), "session") {
			rowCount++
		}
	}
	if rowCount != len(sessions) {
		t.Errorf("expected %d session rows in Fleet view, got %d\ncontent:\n%s", len(sessions), rowCount, content)
	}
}
