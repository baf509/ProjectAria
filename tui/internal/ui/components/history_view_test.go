package components

import (
	"strings"
	"testing"
	"time"

	"github.com/ben/aria-tui/internal/api"
)

// Same class of bug as TestFleetView_AllSessionRowsSurviveAtWideWidth
// (fleet_view_test.go): a lipgloss Render() call with the trailing "\n"
// baked inside it desyncs a bubbles Viewport and silently drops the next
// line. history_view.go was written after that fix landed, but the shape
// (styled header + styled separator immediately above a row loop) is exactly
// what triggered it in Fleet/Health/Memory, so this is worth its own check
// rather than trusting "I was careful" -- especially at a wide terminal,
// which is what actually triggered it live.
func TestHistoryView_AllShellRowsSurviveAtWideWidth(t *testing.T) {
	hv := NewHistoryView()
	hv.SetSize(200, 44)
	now := time.Now()
	var shells []api.ShellRecord
	for i := 0; i < 6; i++ {
		shells = append(shells, api.ShellRecord{
			Name: "claude-coding-abc", ShortName: "coding-abc", Host: "corsair-ai",
			ProjectDir: "/home/ben/Development/ProjectAria", Status: "stopped",
			CreatedAt: now.Add(-time.Duration(i) * time.Hour), LastActivityAt: now,
			LineCount: 1000 + i,
		})
	}
	hv.SetShells(shells)
	content := hv.Viewport.View()

	rowCount := 0
	for _, line := range strings.Split(content, "\n") {
		trimmed := strings.TrimSpace(strings.TrimPrefix(strings.TrimSpace(line), "▸"))
		if strings.HasPrefix(trimmed, "claude-coding") || strings.HasPrefix(trimmed, "coding-abc") {
			rowCount++
		}
	}
	if rowCount != len(shells) {
		t.Errorf("expected %d shell rows in History view, got %d\ncontent:\n%s", len(shells), rowCount, content)
	}
}

func TestHistoryView_FilterMatchesNameAndPath(t *testing.T) {
	hv := NewHistoryView()
	hv.SetSize(120, 40)
	hv.SetShells([]api.ShellRecord{
		{Name: "claude-coding-ridge01", ProjectDir: "/home/ben/Development/war-audio-game"},
		{Name: "claude-coding-other02", ProjectDir: "/home/ben/Development/ProjectAria"},
	})
	hv.Filter.SetValue("war-audio")
	hv.applyFilter()
	if len(hv.Shells) != 1 || hv.Shells[0].Name != "claude-coding-ridge01" {
		t.Fatalf("expected filter to match the war-audio-game shell only, got %+v", hv.Shells)
	}
}
