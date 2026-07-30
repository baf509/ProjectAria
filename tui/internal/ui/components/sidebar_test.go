package components

import (
	"testing"
	"time"

	"github.com/ben/aria-tui/internal/api"
)

func TestFilterSessionsAny(t *testing.T) {
	sessions := []api.CodingSession{
		{ID: "a", Status: "running"},
		{ID: "b", Status: "queued"},
		{ID: "c", Status: "completed"},
		{ID: "d", Status: "failed"},
	}
	got := filterSessionsAny(sessions, "running", "queued")
	if len(got) != 2 {
		t.Fatalf("expected 2 results, got %d", len(got))
	}
	if got[0].ID != "a" || got[1].ID != "b" {
		t.Fatalf("unexpected ids: %s, %s", got[0].ID, got[1].ID)
	}
}

func TestFilterSessionsAnyPointersAliasOriginalSlice(t *testing.T) {
	// Regression: filterSessionsAny must return pointers into the original
	// slice (so ShellName etc. are usable downstream), not copies.
	sessions := []api.CodingSession{{ID: "a", Status: "running", ShellName: "shell-a"}}
	got := filterSessionsAny(sessions, "running")
	got[0].ShellName = "changed"
	if sessions[0].ShellName != "changed" {
		t.Fatalf("expected pointer aliasing, original ShellName = %q", sessions[0].ShellName)
	}
}

// A coding session and the shell backing it are the same live process (every
// backend runs on the shell substrate now, pi-code included) -- SetData must
// render this ONE row, not a coding-session row AND a separate shell row.
func TestSetData_SessionAndItsShellRenderOnce(t *testing.T) {
	s := NewSidebar()
	sessions := []api.CodingSession{
		{ID: "sess-1", Backend: "claude_code", Status: "running", ShellName: "claude-coding-sess1", Prompt: "fix the bug"},
	}
	shells := []api.Shell{
		{Name: "claude-coding-sess1", ShortName: "coding-sess1", ActivityState: "working", Status: "active"},
	}
	s.SetData(nil, nil, sessions, shells)

	var codingSessionCount, shellCount int
	for _, n := range s.Nodes {
		if n.Kind == NodeCodingSession {
			codingSessionCount++
		}
		if n.Kind == NodeShell {
			shellCount++
		}
	}
	if codingSessionCount != 1 {
		t.Errorf("expected 1 coding-session node, got %d", codingSessionCount)
	}
	if shellCount != 0 {
		t.Errorf("expected 0 shell nodes (claimed by the session), got %d", shellCount)
	}
}

// A hand-run shell (no coding_sessions record) is NOT claimed by anything and
// must still show up, in "Your Shells".
func TestSetData_HandRunShellStillShown(t *testing.T) {
	s := NewSidebar()
	shells := []api.Shell{
		{Name: "claude-ProjectAria", ShortName: "ProjectAria", ActivityState: "working", Status: "active"},
	}
	s.SetData(nil, nil, nil, shells)

	found := false
	for _, n := range s.Nodes {
		if n.Kind == NodeShell && n.ID == "claude-ProjectAria" {
			found = true
		}
	}
	if !found {
		t.Fatalf("hand-run shell should still appear as a NodeShell; nodes=%+v", s.Nodes)
	}
}

// Pool and Ridge sessions must land in separate groups from each other and
// from Claude Code, keyed correctly off Backend/LLM (pi-code's LLM field
// distinguishes a Ridge-backed session from a local one -- both share
// Backend="pi-code").
func TestSetData_BackendGrouping(t *testing.T) {
	s := NewSidebar()
	sessions := []api.CodingSession{
		{ID: "pool-1", Backend: "pool", Status: "running", ShellName: "sh-pool"},
		{ID: "ridge-1", Backend: "pi-code", LLM: "ridge", Status: "running", ShellName: "sh-ridge"},
		{ID: "local-1", Backend: "pi-code", LLM: "llamacpp", Status: "running", ShellName: "sh-local"},
		{ID: "claude-1", Backend: "claude_code", Status: "running", ShellName: "sh-claude"},
	}
	s.SetData(nil, nil, sessions, nil)

	labels := sectionLabels(s.Nodes)
	mustContainSubstring(t, labels, "Pool (1/1 active)")
	mustContainSubstring(t, labels, "Ridge (1/1 active)")
	mustContainSubstring(t, labels, "Claude Code (1 active)")
	mustContainSubstring(t, labels, "Local Pi-Code (1 active)")
}

// A queued session must count toward the group's total but display as
// "queued", not silently look like it's running when it isn't.
func TestSetData_QueuedSessionShowsInLabel(t *testing.T) {
	s := NewSidebar()
	sessions := []api.CodingSession{
		{ID: "pool-1", Backend: "pool", Status: "running", ShellName: "sh-pool"},
		{ID: "pool-2", Backend: "pool", Status: "queued"},
	}
	s.SetData(nil, nil, sessions, nil)

	labels := sectionLabels(s.Nodes)
	mustContainSubstring(t, labels, "Pool (1/1 active · 1 queued)")
}

// Conversations must nest under their owning agent, not sit in a flat
// top-level list disconnected from it.
func TestSetData_ConversationsNestUnderTheirAgent(t *testing.T) {
	s := NewSidebar()
	agents := []api.Agent{
		{ID: "agent-1", Slug: "pi-coding-ridge", Name: "Pi Coding Agent (Ridge)", IsDefault: false},
	}
	convs := []api.Conversation{
		{ID: "conv-1", AgentID: "agent-1", Title: "Ridge agent smoke test", UpdatedAt: time.Now()},
	}
	s.SetData(agents, convs, nil, nil)

	agentIdx, convIdx := -1, -1
	for i, n := range s.Nodes {
		if n.Kind == NodeAgent && n.ID == "pi-coding-ridge" {
			agentIdx = i
		}
		if n.Kind == NodeConversation && n.ID == "conv-1" {
			convIdx = i
		}
	}
	if agentIdx == -1 || convIdx == -1 {
		t.Fatalf("expected both agent and conversation nodes; nodes=%+v", s.Nodes)
	}
	if convIdx != agentIdx+1 {
		t.Errorf("expected conversation immediately after its agent (agent@%d, conv@%d)", agentIdx, convIdx)
	}
	if s.Nodes[convIdx].Depth <= s.Nodes[agentIdx].Depth {
		t.Errorf("expected conversation to be indented deeper than its agent (agent depth=%d, conv depth=%d)",
			s.Nodes[agentIdx].Depth, s.Nodes[convIdx].Depth)
	}
}

// A conversation whose agent was deleted/renamed must not be silently
// dropped -- it surfaces in "Other Conversations" instead.
func TestSetData_OrphanConversationSurfacesSeparately(t *testing.T) {
	s := NewSidebar()
	convs := []api.Conversation{
		{ID: "conv-orphan", AgentID: "does-not-exist", Title: "stale", UpdatedAt: time.Now()},
	}
	s.SetData(nil, convs, nil, nil)

	found := false
	for _, n := range s.Nodes {
		if n.Kind == NodeConversation && n.ID == "conv-orphan" {
			found = true
		}
	}
	if !found {
		t.Fatalf("orphan conversation must still appear; nodes=%+v", s.Nodes)
	}
	mustContainSubstring(t, sectionLabels(s.Nodes), "Other Conversations (1)")
}

func sectionLabels(nodes []TreeNode) []string {
	var out []string
	for _, n := range nodes {
		if n.Kind == NodeSection {
			out = append(out, n.Label)
		}
	}
	return out
}

func mustContainSubstring(t *testing.T, haystack []string, want string) {
	t.Helper()
	for _, h := range haystack {
		if h == want {
			return
		}
	}
	t.Errorf("expected a section labeled %q, got %+v", want, haystack)
}
