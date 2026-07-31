package ui

import (
	"testing"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/components"
	tea "github.com/charmbracelet/bubbletea"
)

func newTestModel() Model {
	m := NewModel(api.NewClient("http://127.0.0.1:9/", ""))
	m.width, m.height, m.ready = 120, 40, true
	m.layout()
	return m
}

func runeKey(s string) tea.KeyMsg { return tea.KeyMsg{Type: tea.KeyRunes, Runes: []rune(s)} }

// Dashboard hotkeys (bare and via default branch) must switch screens.
func TestDashboardHotkeyOpensScreen(t *testing.T) {
	m := newTestModel()
	if cmd, consumed := m.handleKey(runeKey("f")); !consumed || cmd == nil {
		t.Fatalf("f: consumed=%v cmd==nil=%v", consumed, cmd == nil)
	}
	if m.screen != screenFleet {
		t.Fatalf("expected screenFleet, got %v", m.screen)
	}
}

// Tab toggles focus only between the two interactive left panes.
func TestTabTogglesLeftPanes(t *testing.T) {
	m := newTestModel()
	if m.quad != quadTopLeft {
		t.Fatalf("initial quad = %v", m.quad)
	}
	m.handleKey(tea.KeyMsg{Type: tea.KeyTab})
	if m.quad != quadBotLeft {
		t.Fatalf("after tab, quad = %v want quadBotLeft", m.quad)
	}
	m.handleKey(tea.KeyMsg{Type: tea.KeyTab})
	if m.quad != quadTopLeft {
		t.Fatalf("after 2nd tab, quad = %v want quadTopLeft", m.quad)
	}
}

// With the Tools menu focused, arrows move its cursor and Enter launches the
// highlighted entry (using the same openHotkey path as the bare hotkey).
func TestToolsMenuEnterLaunches(t *testing.T) {
	m := newTestModel()
	m.handleKey(tea.KeyMsg{Type: tea.KeyTab}) // focus tools menu
	// Move cursor to the "m" (Memories) entry.
	for m.menu.Selected().Key != "m" {
		before := m.menu.Cursor
		m.handleKey(runeKey("j"))
		if m.menu.Cursor == before {
			t.Fatalf("cursor stuck at %d (%q)", m.menu.Cursor, m.menu.Selected().Key)
		}
	}
	m.handleKey(tea.KeyMsg{Type: tea.KeyEnter})
	if m.screen != screenMemory {
		t.Fatalf("expected screenMemory, got %v", m.screen)
	}
}

// A handled key (Enter submit) is consumed; ordinary typing is NOT consumed so
// it falls through to the textarea (this is the stray-newline fix).
func TestChatKeyConsumption(t *testing.T) {
	m := newTestModel()
	m.screen = screenChat
	// Empty input: Enter is consumed (no forward → no stray newline) but yields
	// no command.
	cmd, consumed := m.handleKey(tea.KeyMsg{Type: tea.KeyEnter})
	if !consumed || cmd != nil {
		t.Fatalf("empty enter: consumed=%v cmd==nil=%v", consumed, cmd == nil)
	}
	// A letter is not an app action → not consumed → forwarded to textarea.
	if _, consumed := m.handleKey(runeKey("a")); consumed {
		t.Fatalf("letter 'a' should not be consumed on chat screen")
	}
}

// Session actions are ctrl-modified so bare letters remain typeable in the
// session input box.
func TestSessionActionKeys(t *testing.T) {
	m := newTestModel()
	m.screen = screenSession
	m.activeSessionID = "sess1"
	if _, consumed := m.handleKey(tea.KeyMsg{Type: tea.KeyCtrlS}); !consumed {
		t.Fatalf("ctrl+s should be consumed on session screen")
	}
	// A bare 's' must NOT be an action anymore — it's typing.
	if _, consumed := m.handleKey(runeKey("s")); consumed {
		t.Fatalf("bare 's' should be typeable (not consumed) on session screen")
	}
}

// Esc backs out of a sub-screen.
func TestEscPopsScreen(t *testing.T) {
	m := newTestModel()
	m.handleKey(runeKey("h")) // open Health
	if m.screen != screenHealth {
		t.Fatalf("expected screenHealth, got %v", m.screen)
	}
	m.handleKey(tea.KeyMsg{Type: tea.KeyEsc})
	if m.screen != screenDashboard {
		t.Fatalf("esc should return to dashboard, got %v", m.screen)
	}
}

// The sidebar keeps the same logical selection across a data refresh even when
// the row order changes -- using Claude Code sessions (2026-07-31 restructure:
// bare conversations no longer render as selectable sidebar nodes at all, so
// this now exercises the same guarantee against coding-session nodes instead).
func TestSidebarPreservesSelectionAcrossRefresh(t *testing.T) {
	sb := components.NewSidebar()
	sb.SetSize(30, 20)
	sessions := []api.CodingSession{
		{ID: "c1", Backend: "claude_code", Status: "running"},
		{ID: "c2", Backend: "claude_code", Status: "running"},
		{ID: "c3", Backend: "claude_code", Status: "running"},
	}
	sb.SetData(nil, nil, sessions, nil)
	// Select c2. Bounded loop, not `for Selected()...Down()` unbounded -- a
	// node genuinely not being reachable must fail loudly, not spin forever.
	found := false
	for i := 0; i < 50; i++ {
		if sel := sb.Selected(); sel != nil && sel.ID == "c2" {
			found = true
			break
		}
		before := sb.Cursor
		sb.Down()
		if sb.Cursor == before {
			break // hit the end of the list
		}
	}
	if !found {
		t.Fatalf("setup: could not select c2")
	}
	// Refresh with reordered list.
	reordered := []api.CodingSession{
		{ID: "c3", Backend: "claude_code", Status: "running"},
		{ID: "c2", Backend: "claude_code", Status: "running"},
		{ID: "c1", Backend: "claude_code", Status: "running"},
	}
	sb.SetData(nil, nil, reordered, nil)
	if sel := sb.Selected(); sel == nil || sel.ID != "c2" {
		t.Fatalf("selection not preserved: got %v", sel)
	}
}
