package components

import (
	"strings"
	"testing"
	"time"

	"github.com/ben/aria-tui/internal/api"
)

func piLocalAgent() api.Agent {
	a := api.Agent{ID: "a-local", Slug: "pi-coding", Name: "Pi Coding Agent (Local)", Enabled: true}
	a.LLM.Backend = "agentic"
	a.LLM.Model = "chadrockv2-qwen36-27b-fp6"
	return a
}

func piRidgeAgent() api.Agent {
	a := api.Agent{ID: "a-ridge", Slug: "pi-coding-ridge", Name: "Pi Coding Agent (Ridge)", Enabled: true}
	a.LLM.Backend = "ridge"
	a.LLM.Model = "qwen3.6-35b-a3b"
	return a
}

// Sessions must be grouped by which db.agents row they belong to (matched via
// Backend="pi-code" + LLM == that agent's own llm.backend), not by a
// hardcoded backend string -- pi-coding's own backend has already changed
// once this cycle (chadrock/pool -> agentic), and grouping by raw string
// silently stopped matching real sessions when that happened.
func TestSetData_PiSessionsGroupByAgentSlugNotBackendString(t *testing.T) {
	s := NewSidebar()
	agents := []api.Agent{piLocalAgent(), piRidgeAgent()}
	sessions := []api.CodingSession{
		{ID: "local-1", Backend: "pi-code", LLM: "agentic", Status: "running"},
		{ID: "ridge-1", Backend: "pi-code", LLM: "ridge", Status: "running"},
	}
	s.SetData(agents, nil, sessions, nil)

	var localGroupIdx, ridgeGroupIdx = -1, -1
	for i, n := range s.Nodes {
		if n.Kind == NodeCodingAgentGroup && n.SessionProfile == "pi-coding" {
			localGroupIdx = i
		}
		if n.Kind == NodeCodingAgentGroup && n.SessionProfile == "pi-coding-ridge" {
			ridgeGroupIdx = i
		}
	}
	if localGroupIdx == -1 || ridgeGroupIdx == -1 {
		t.Fatalf("expected both Pi agent group headers; nodes=%+v", s.Nodes)
	}
	if s.Nodes[localGroupIdx+1].ID != "local-1" {
		t.Errorf("expected local-1 right after the Local group header, got %+v", s.Nodes[localGroupIdx+1])
	}
	if s.Nodes[ridgeGroupIdx+1].ID != "ridge-1" {
		t.Errorf("expected ridge-1 right after the Ridge group header, got %+v", s.Nodes[ridgeGroupIdx+1])
	}
}

// A Pi agent group always renders even with zero sessions -- otherwise
// there'd be no row to hit Enter on to start a first session under it.
func TestSetData_PiAgentGroupShownWithZeroSessions(t *testing.T) {
	s := NewSidebar()
	s.SetData([]api.Agent{piLocalAgent()}, nil, nil, nil)

	found := false
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingAgentGroup && n.SessionProfile == "pi-coding" {
			found = true
			if n.Label == "" {
				t.Errorf("expected a non-empty header label")
			}
		}
	}
	if !found {
		t.Fatalf("expected the Pi Local group header even with no sessions; nodes=%+v", s.Nodes)
	}
}

// The single-consumer ceiling (chadrockv2 and Ridge/NInfer are both
// --parallel 1) must stay visible in the header, and the model label must be
// sanitized, not the raw internal alias.
func TestSetData_PiAgentHeaderShowsActiveCeilingAndModelLabel(t *testing.T) {
	s := NewSidebar()
	sessions := []api.CodingSession{
		{ID: "local-1", Backend: "pi-code", LLM: "agentic", Status: "running"},
	}
	s.SetData([]api.Agent{piLocalAgent()}, nil, sessions, nil)

	var header string
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingAgentGroup && n.SessionProfile == "pi-coding" {
			header = n.Label
		}
	}
	if !strings.Contains(header, "1/1 active") {
		t.Errorf("expected %q to contain '1/1 active'", header)
	}
	if !strings.Contains(header, "Qwen3.6 27B") {
		t.Errorf("expected %q to contain the sanitized model label 'Qwen3.6 27B'", header)
	}
}

// Only the 3 most recent/active sessions render per Pi agent -- "Your
// Shells" grew to 297 entries with no cap; this must not repeat that.
func TestSetData_PiAgentSessionsCappedAtThree(t *testing.T) {
	s := NewSidebar()
	now := time.Now()
	sessions := []api.CodingSession{
		{ID: "s1", Backend: "pi-code", LLM: "agentic", Status: "completed", UpdatedAt: now.Add(-4 * time.Hour)},
		{ID: "s2", Backend: "pi-code", LLM: "agentic", Status: "completed", UpdatedAt: now.Add(-3 * time.Hour)},
		{ID: "s3", Backend: "pi-code", LLM: "agentic", Status: "completed", UpdatedAt: now.Add(-2 * time.Hour)},
		{ID: "s4", Backend: "pi-code", LLM: "agentic", Status: "completed", UpdatedAt: now.Add(-1 * time.Hour)},
		{ID: "s5", Backend: "pi-code", LLM: "agentic", Status: "running", UpdatedAt: now},
	}
	s.SetData([]api.Agent{piLocalAgent()}, nil, sessions, nil)

	var shown []string
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingSession {
			shown = append(shown, n.ID)
		}
	}
	if len(shown) != 3 {
		t.Fatalf("expected 3 sessions shown, got %d: %v", len(shown), shown)
	}
	// The active one (s5) must be included despite not being most-recently
	// updated among all 5 -- active always outranks merely-recent.
	if shown[0] != "s5" {
		t.Errorf("expected the running session (s5) first, got %v", shown)
	}
}

// Claude Code and Codex are unbounded/cloud -- every matching session shows,
// no cap, and the group renders even with zero sessions (same reasoning as
// the Pi agents: Enter needs somewhere to start a first one from).
func TestSetData_ClaudeCodeAndCodexUncapped(t *testing.T) {
	s := NewSidebar()
	var sessions []api.CodingSession
	for i := 0; i < 5; i++ {
		sessions = append(sessions, api.CodingSession{ID: "c" + string(rune('a'+i)), Backend: "claude_code", Status: "running"})
	}
	s.SetData(nil, nil, sessions, nil)

	count := 0
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingSession {
			count++
		}
	}
	if count != 5 {
		t.Errorf("expected all 5 Claude Code sessions shown uncapped, got %d", count)
	}

	s2 := NewSidebar()
	s2.SetData(nil, nil, nil, nil)
	foundClaude, foundCodex := false, false
	for _, n := range s2.Nodes {
		if n.Kind == NodeCodingAgentGroup && n.SessionBackend == "claude_code" {
			foundClaude = true
		}
		if n.Kind == NodeCodingAgentGroup && n.SessionBackend == "codex" {
			foundCodex = true
		}
	}
	if !foundClaude || !foundCodex {
		t.Fatalf("expected Claude Code and Codex headers even with zero sessions; nodes=%+v", s2.Nodes)
	}
}

// Claude Code and Codex live under their own "Cloud Coding Agents" section,
// as siblings of the Pi agent groups under "Local Coding Agents" -- not
// top-level headers outside any section. Their group-header depth must match
// a Pi agent group header's depth so the two families read as equals.
func TestSetData_CloudAgentsNestedUnderOwnSection(t *testing.T) {
	s := NewSidebar()
	s.SetData([]api.Agent{piLocalAgent()}, nil, nil, nil)

	var sawLocalSection, sawCloudSection bool
	var localGroupDepth, claudeDepth, codexDepth int
	haveClaudeDepth, haveCodexDepth := false, false
	for _, n := range s.Nodes {
		switch {
		case n.Kind == NodeSection && n.Label == "Local Coding Agents":
			sawLocalSection = true
		case n.Kind == NodeSection && n.Label == "Cloud Coding Agents":
			sawCloudSection = true
		case n.Kind == NodeCodingAgentGroup && n.SessionProfile == "pi-coding":
			localGroupDepth = n.Depth
		case n.Kind == NodeCodingAgentGroup && n.SessionBackend == "claude_code":
			claudeDepth = n.Depth
			haveClaudeDepth = true
		case n.Kind == NodeCodingAgentGroup && n.SessionBackend == "codex":
			codexDepth = n.Depth
			haveCodexDepth = true
		}
	}
	if !sawLocalSection || !sawCloudSection {
		t.Fatalf("expected both 'Local Coding Agents' and 'Cloud Coding Agents' section headers; nodes=%+v", s.Nodes)
	}
	if !haveClaudeDepth || !haveCodexDepth {
		t.Fatalf("expected Claude Code and Codex group headers; nodes=%+v", s.Nodes)
	}
	if claudeDepth != localGroupDepth || codexDepth != localGroupDepth {
		t.Errorf("expected Claude Code/Codex depth (%d/%d) to match Pi Local group depth (%d)", claudeDepth, codexDepth, localGroupDepth)
	}
}

// A disabled agent (Search Agent while paused) is shown greyed, not hidden --
// re-enabling it shouldn't feel like the row appeared from nowhere.
func TestSetData_DisabledAgentShownNotHidden(t *testing.T) {
	s := NewSidebar()
	agents := []api.Agent{
		{ID: "a-search", Slug: "search-agent", Name: "Search Agent", Enabled: false},
	}
	s.SetData(agents, nil, nil, nil)

	var found *TreeNode
	for i := range s.Nodes {
		if s.Nodes[i].ID == "search-agent" {
			found = &s.Nodes[i]
		}
	}
	if found == nil {
		t.Fatalf("disabled agent must still appear; nodes=%+v", s.Nodes)
	}
	if found.Status != "disabled" {
		t.Errorf("expected Status=disabled, got %q", found.Status)
	}
}

// Hermes has no API surface for this TUI to drive yet -- it's stubbed in
// (visible, marked not-yet-available) rather than omitted entirely.
func TestSetData_HermesStubAlwaysPresent(t *testing.T) {
	s := NewSidebar()
	s.SetData(nil, nil, nil, nil)

	found := false
	for _, n := range s.Nodes {
		if n.ID == "hermes-agent-stub" {
			found = true
			if !n.IsStub {
				t.Errorf("expected IsStub=true on the Hermes placeholder")
			}
		}
	}
	if !found {
		t.Fatalf("expected the Hermes stub row; nodes=%+v", s.Nodes)
	}
}

// Regression: a first live run against real data showed Claude Code
// rendering all ~94 historical sessions ever created, not just active ones
// -- the restructure dropped the active-only filter the original code
// applied before grouping. Completed/failed/stopped sessions must not render.
func TestSetData_ClaudeCodeExcludesFinishedSessions(t *testing.T) {
	s := NewSidebar()
	sessions := []api.CodingSession{
		{ID: "running-1", Backend: "claude_code", Status: "running"},
		{ID: "queued-1", Backend: "claude_code", Status: "queued"},
		{ID: "done-1", Backend: "claude_code", Status: "completed"},
		{ID: "failed-1", Backend: "claude_code", Status: "failed"},
		{ID: "stopped-1", Backend: "claude_code", Status: "stopped"},
	}
	s.SetData(nil, nil, sessions, nil)

	var shown []string
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingSession {
			shown = append(shown, n.ID)
		}
	}
	if len(shown) != 2 {
		t.Fatalf("expected only the 2 active (running/queued) sessions shown, got %d: %v", len(shown), shown)
	}
}

func TestFormatModelLabel(t *testing.T) {
	cases := map[string]string{
		"chadrockv2-qwen36-27b-fp6": "Qwen3.6 27B",
		"qwen3.6-35b-a3b":           "Qwen3.6 35B",
		"qwen35b-a3b-mtp":           "Qwen3.6 35B",
		"":                          "",
		"some-future-model-id":      "some-future-model-id", // unrecognized passes through, not blank
	}
	for raw, want := range cases {
		if got := formatModelLabel(raw); got != want {
			t.Errorf("formatModelLabel(%q) = %q, want %q", raw, got, want)
		}
	}
}
