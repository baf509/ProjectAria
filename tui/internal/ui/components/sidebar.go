package components

import (
	"fmt"
	"strings"
	"time"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/lipgloss"
)

// TreeNodeKind identifies what a sidebar entry represents.
type TreeNodeKind int

const (
	NodeSection          TreeNodeKind = iota // Non-selectable collapsible header
	NodeAgent                                // Other Agents entry: Search Agent (chat-style) or the Hermes stub
	NodeConversation                         // Existing conversation (an Other Agents chat-style entry)
	NodeCodingSession                        // A coding session (running/queued/recent)
	NodeShell                                // Watched tmux shell -- used by the Fleet screen's data, not this tree
	NodeCodingAgentGroup                     // Selectable header for a coding-session family (Pi Local/Ridge/Claude Code/Codex). Enter opens the New Session modal pre-filled for it.
)

// TreeNode is a single row in the sidebar tree.
type TreeNode struct {
	ID        string
	Label     string
	Kind      TreeNodeKind
	Status    string // lifecycle status for icon
	Category  string // "chat", "coding", "research"
	AgentSlug string
	Meta      string // secondary info (model, workspace, etc.)
	Depth     int    // indentation level
	Children  int    // count of children (for section headers)

	// New Session modal pre-fill -- set on NodeCodingAgentGroup nodes only.
	// Exactly one of SessionBackend/SessionProfile is non-empty.
	SessionBackend string // "claude_code" | "codex"
	SessionProfile string // "pi-coding" | "pi-coding-ridge" (resolves a db.agents specialist)

	// IsStub marks a placeholder entry with no live backend yet (the Hermes
	// Agent row) -- Enter shows a "not yet available" message instead of
	// acting, rather than silently doing nothing or erroring against an API
	// call that was never going to succeed.
	IsStub bool

	// Original data pointers for detail views
	Agent         *api.Agent
	Conversation  *api.Conversation
	CodingSession *api.CodingSession
	Shell         *api.Shell
}

type Sidebar struct {
	Nodes   []TreeNode
	Cursor  int
	Offset  int
	Height  int
	Width   int
	Focused bool
	Filter  string // future: text filter
}

func NewSidebar() *Sidebar {
	return &Sidebar{}
}

func (s *Sidebar) SetSize(w, h int) {
	s.Width = w
	s.Height = h
}

func (s *Sidebar) Up() {
	for {
		if s.Cursor <= 0 {
			break
		}
		s.Cursor--
		// Skip section headers
		if s.Nodes[s.Cursor].Kind != NodeSection {
			break
		}
	}
	if s.Cursor < s.Offset {
		s.Offset = s.Cursor
	}
}

func (s *Sidebar) Down() {
	for {
		if s.Cursor >= len(s.Nodes)-1 {
			break
		}
		s.Cursor++
		if s.Nodes[s.Cursor].Kind != NodeSection {
			break
		}
	}
	visible := s.visibleCount()
	if s.Cursor >= s.Offset+visible {
		s.Offset = s.Cursor - visible + 1
	}
}

func (s *Sidebar) Selected() *TreeNode {
	if s.Cursor >= 0 && s.Cursor < len(s.Nodes) {
		return &s.Nodes[s.Cursor]
	}
	return nil
}

func (s *Sidebar) visibleCount() int {
	return max(1, s.Height-4) // border + title + separator
}

// SetData rebuilds the tree from API data.
//
// Structure (2026-07-30 restructuring: split "Coding Agents" into Local vs
// Cloud sections, so the local/self-hosted-model agents and the cloud-API
// agents aren't visually flattened into one undifferentiated list — grouping
// coding sessions by db.agents SLUG rather than raw backend string, unchanged
// from the prior pass, since that's what survives pi-coding's model moving
// between backends):
//
//	▸ Local Coding Agents             -- non-selectable umbrella label
//	  ⊕ Pi Coding Agent (Local) — <model>   (x/1 active)   -- Enter: new session
//	    session, session, session (active first, capped at 3)
//	  ⊕ Pi Coding Agent (Ridge) — <model>   (x/1 active)   -- Enter: new session
//	    session, session, session (active first, capped at 3)
//	▸ Cloud Coding Agents             -- non-selectable umbrella label
//	  ⊕ Claude Code (N active)        -- Enter: new session; uncapped, unbounded concurrency
//	  ⊕ Codex (N active)              -- Enter: new session; uncapped, unbounded concurrency
//	▸ Other Agents                    -- non-selectable umbrella label
//	  Search Agent                    -- greyed out while disabled in db.agents
//	  Hermes Agent                    -- stub; Hermes has no API surface to drive yet
//
// A coding session and the shell backing it are the same live process (pi-code
// runs on the shell substrate too) -- rendered as ONE row per session, not two.
func (s *Sidebar) SetData(agents []api.Agent, convs []api.Conversation, sessions []api.CodingSession, shells []api.Shell) {
	_ = convs  // Other Agents' chat-style entries no longer nest conversations inline; kept for signature stability.
	_ = shells // shell activity_state enrichment removed with "Your Shells" — sessions show coding_sessions.Status directly.

	// Remember what was selected so the 3s refresh doesn't yank the cursor onto
	// a different row (which would make Enter open the wrong thing).
	var prevID string
	var prevKind TreeNodeKind
	if s.Cursor >= 0 && s.Cursor < len(s.Nodes) {
		prevID = s.Nodes[s.Cursor].ID
		prevKind = s.Nodes[s.Cursor].Kind
	}

	s.Nodes = nil

	// Live agent lookup by slug -- the whole point of grouping this way
	// instead of matching backend/LLM strings is that it keeps working when
	// an agent gets repointed at a different model server.
	var piLocalAgent, piRidgeAgent *api.Agent
	var otherAgents []*api.Agent
	for i := range agents {
		a := &agents[i]
		switch {
		case a.IsDefault:
			// ARIA herself -- the coordinator, not a delegated agent. Excluded
			// from every group, same as before.
		case a.Slug == "pi-coding":
			piLocalAgent = a
		case a.Slug == "pi-coding-ridge":
			piRidgeAgent = a
		default:
			otherAgents = append(otherAgents, a)
		}
	}

	// --- Local Coding Agents: Pi Local + Pi Ridge, always shown when the
	// agent exists (even with zero sessions -- otherwise there'd be no way to
	// start a FIRST session under one from here). ---
	if piLocalAgent != nil || piRidgeAgent != nil {
		s.Nodes = append(s.Nodes, TreeNode{Kind: NodeSection, Label: "Local Coding Agents"})
		if piLocalAgent != nil {
			s.appendCodingAgentGroup("Pi Coding Agent (Local)", piLocalAgent, sessions, 1)
		}
		if piRidgeAgent != nil {
			s.appendCodingAgentGroup("Pi Coding Agent (Ridge)", piRidgeAgent, sessions, 1)
		}
	}

	// --- Cloud Coding Agents: Claude Code / Codex, unbounded concurrency, not
	// tied to a single-consumer model server -- every ACTIVE matching session
	// shown, no cap (unlike the Pi agents' cap-of-3, there's no ceiling to
	// respect here). Nested at the same depth as the Pi agent group headers
	// under their own section, not top-level, so the two coding-agent
	// families read as siblings rather than Claude Code looking more
	// "important" by sitting outside any group. Active-only, not "no cap at
	// all": `sessions` is every session ever created for this workspace,
	// active or finished months ago -- the original code filtered this
	// before grouping and an earlier restructure dropped that filter, so
	// this rendered all ~94 historical sessions on first live test. Same
	// class of bug "Your Shells" had at 297 entries.
	var claudeSessions, codexSessions []*api.CodingSession
	for i := range sessions {
		cs := &sessions[i]
		if cs.Status != "running" && cs.Status != "queued" {
			continue
		}
		switch cs.Backend {
		case "claude_code":
			claudeSessions = append(claudeSessions, cs)
		case "codex":
			codexSessions = append(codexSessions, cs)
		}
	}
	s.Nodes = append(s.Nodes, TreeNode{Kind: NodeSection, Label: "Cloud Coding Agents"})
	s.appendTopLevelGroup("Claude Code", "claude_code", "", claudeSessions, 1)
	s.appendTopLevelGroup("Codex", "codex", "", codexSessions, 1)

	// --- Other Agents: everything else that isn't a coding specialist --
	// e.g. Search Agent -- shown even while disabled (greyed, not hidden, so
	// re-enabling it doesn't feel like the entry appeared from nowhere) --
	// plus a stub for Hermes: it has no API surface yet for this TUI to
	// drive, so Enter explains that instead of either doing nothing or
	// failing against a call that was never going to work.
	s.Nodes = append(s.Nodes, TreeNode{Kind: NodeSection, Label: "Other Agents"})
	for _, a := range otherAgents {
		status := "idle"
		if !a.Enabled {
			status = "disabled"
		}
		s.Nodes = append(s.Nodes, TreeNode{
			ID:        a.Slug,
			Label:     a.Name,
			Kind:      NodeAgent,
			Category:  a.ModeCategory,
			AgentSlug: a.Slug,
			Meta:      fmt.Sprintf("%s/%s", a.LLM.Backend, a.LLM.Model),
			Status:    status,
			Depth:     1,
			Agent:     a,
		})
	}
	s.Nodes = append(s.Nodes, TreeNode{
		ID:     "hermes-agent-stub",
		Label:  "Hermes Agent",
		Kind:   NodeAgent,
		Status: "disabled",
		Meta:   "not yet available",
		Depth:  1,
		IsStub: true,
	})

	// Restore the previous selection by identity if it still exists; otherwise
	// fall back to clamping onto a selectable node.
	if prevID != "" {
		for i := range s.Nodes {
			if s.Nodes[i].ID == prevID && s.Nodes[i].Kind == prevKind {
				s.Cursor = i
				break
			}
		}
	}
	s.fixCursor()
	s.ensureVisible()
}

// appendCodingAgentGroup renders one Pi Coding Agent as a selectable header
// ("x/1 active" -- chadrockv2 and Ridge/NInfer are both --parallel 1, a real
// single-consumer ceiling, not a display choice) followed by up to 3 of its
// sessions: active/queued first, then most-recently-updated, so the group
// stays useful without turning into an unbounded scrollback the way "Your
// Shells" did at 297 entries.
func (s *Sidebar) appendCodingAgentGroup(label string, agent *api.Agent, sessions []api.CodingSession, depth int) {
	var mine []*api.CodingSession
	for i := range sessions {
		cs := &sessions[i]
		if cs.Backend == "pi-code" && cs.LLM == agent.LLM.Backend {
			mine = append(mine, cs)
		}
	}
	sortSessionsForDisplay(mine)

	activeCount := 0
	for _, cs := range mine {
		if cs.Status == "running" || cs.Status == "queued" {
			activeCount++
		}
	}
	modelLabel := formatModelLabel(agent.LLM.Model)
	header := label
	if modelLabel != "" {
		header = fmt.Sprintf("%s — %s", label, modelLabel)
	}
	header = fmt.Sprintf("%s (%d/1 active)", header, activeCount)

	s.Nodes = append(s.Nodes, TreeNode{
		ID:             "coding-agent:" + agent.Slug,
		Label:          header,
		Kind:           NodeCodingAgentGroup,
		Category:       "coding",
		SessionProfile: agent.Slug,
		Depth:          depth,
		Agent:          agent,
	})
	shown := mine
	if len(shown) > 3 {
		shown = shown[:3]
	}
	for _, cs := range shown {
		s.Nodes = append(s.Nodes, codingSessionNode(cs, nil, depth+1))
	}
}

// appendTopLevelGroup renders a cloud coding-session family (Claude Code,
// Codex) as a selectable, uncapped header + all matching sessions. Always
// shown, even with zero sessions -- otherwise Enter (start a new one) would
// have nowhere to live until the first session already existed.
func (s *Sidebar) appendTopLevelGroup(label, backend, profile string, sessions []*api.CodingSession, depth int) {
	activeCount, queuedCount := 0, 0
	for _, cs := range sessions {
		if cs.Status == "queued" {
			queuedCount++
		} else if cs.Status == "running" {
			activeCount++
		}
	}
	header := fmt.Sprintf("%s (%d active", label, activeCount)
	if queuedCount > 0 {
		header += fmt.Sprintf(" · %d queued", queuedCount)
	}
	header += ")"

	s.Nodes = append(s.Nodes, TreeNode{
		ID:             "coding-agent:" + backend + profile,
		Label:          header,
		Kind:           NodeCodingAgentGroup,
		Category:       "coding",
		SessionBackend: backend,
		SessionProfile: profile,
		Depth:          depth,
	})
	sortSessionsForDisplay(sessions)
	for _, cs := range sessions {
		s.Nodes = append(s.Nodes, codingSessionNode(cs, nil, depth+1))
	}
}

// sortSessionsForDisplay orders active/queued sessions before finished ones,
// most-recently-updated first within each group -- simple insertion sort,
// these lists are never more than a handful of items.
func sortSessionsForDisplay(sessions []*api.CodingSession) {
	rank := func(cs *api.CodingSession) int {
		if cs.Status == "running" || cs.Status == "queued" {
			return 0
		}
		return 1
	}
	for i := 1; i < len(sessions); i++ {
		for j := i; j > 0; j-- {
			a, b := sessions[j-1], sessions[j]
			swap := rank(a) > rank(b)
			if rank(a) == rank(b) && b.UpdatedAt.After(a.UpdatedAt) {
				swap = true
			}
			if !swap {
				break
			}
			sessions[j-1], sessions[j] = sessions[j], sessions[j-1]
		}
	}
}

// codingSessionNode renders a coding session as one row. When its shell is
// known, the shell's activity_state drives status/meta (the more real-time
// signal); otherwise falls back to the coding_sessions.Status field.
func codingSessionNode(cs *api.CodingSession, shell *api.Shell, depth int) TreeNode {
	label := truncate(cs.Prompt, 30)
	if label == "" {
		label = cs.ID[:min(8, len(cs.ID))]
	}
	status := cs.Status
	meta := cs.Backend
	if shell != nil {
		meta = relativeIdle(shell.IdleSeconds)
		switch shell.ActivityState {
		case "blocked":
			status = "blocked"
			meta = "⏳ blocked"
		case "done":
			status = "done"
			meta = "✓ done"
		case "working":
			status = "running"
		}
	}
	return TreeNode{
		ID:            cs.ID,
		Label:         label,
		Kind:          NodeCodingSession,
		Category:      "coding",
		Status:        status,
		Meta:          meta,
		Depth:         depth,
		CodingSession: cs,
		Shell:         shell,
	}
}

func conversationNode(c *api.Conversation, category, agentSlug string, depth int) TreeNode {
	for _, tag := range c.Tags {
		if tag == "pi-coding" || tag == "coding" {
			category = "coding"
			break
		}
		if tag == "research" {
			category = "research"
			break
		}
	}
	title := c.Title
	if title == "" {
		title = c.ID[:min(8, len(c.ID))]
	}
	if c.Private {
		title = "[private] " + title
	}
	return TreeNode{
		ID:           c.ID,
		Label:        title,
		Kind:         NodeConversation,
		Category:     category,
		AgentSlug:    agentSlug,
		Status:       c.Status,
		Meta:         relativeTime(c.UpdatedAt),
		Depth:        depth,
		Conversation: c,
	}
}

// ensureVisible clamps Offset so the cursor stays on screen after the node list
// is rebuilt (it can grow or shrink between refreshes).
func (s *Sidebar) ensureVisible() {
	visible := s.visibleCount()
	if s.Cursor < s.Offset {
		s.Offset = s.Cursor
	} else if s.Cursor >= s.Offset+visible {
		s.Offset = s.Cursor - visible + 1
	}
	maxOff := len(s.Nodes) - visible
	if maxOff < 0 {
		maxOff = 0
	}
	if s.Offset > maxOff {
		s.Offset = maxOff
	}
	if s.Offset < 0 {
		s.Offset = 0
	}
}

// Legacy compat
func (s *Sidebar) SetConversations(convs []api.Conversation, agents []api.Agent) {
	s.SetData(agents, convs, nil, nil)
}

func (s *Sidebar) fixCursor() {
	if len(s.Nodes) == 0 {
		s.Cursor = 0
		return
	}
	if s.Cursor >= len(s.Nodes) {
		s.Cursor = len(s.Nodes) - 1
	}
	// Move past section headers
	if s.Nodes[s.Cursor].Kind == NodeSection {
		for i := s.Cursor; i < len(s.Nodes); i++ {
			if s.Nodes[i].Kind != NodeSection {
				s.Cursor = i
				return
			}
		}
		// All remaining are sections? Go backward
		for i := s.Cursor; i >= 0; i-- {
			if s.Nodes[i].Kind != NodeSection {
				s.Cursor = i
				return
			}
		}
	}
}

// RenderContent returns the sidebar's inner content without any border wrapping.
// Used by the dashboard's 4-quadrant layout which applies its own panel border.
func (s *Sidebar) RenderContent() string {
	if len(s.Nodes) == 0 {
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("  No agents or conversations")
	}

	contentWidth := s.Width - 4
	if contentWidth < 10 {
		contentWidth = 10
	}

	var b strings.Builder

	visible := s.visibleCount()
	end := s.Offset + visible
	if end > len(s.Nodes) {
		end = len(s.Nodes)
	}

	lines := 0
	for i := s.Offset; i < end; i++ {
		node := s.Nodes[i]
		line := s.renderNode(i, node, contentWidth)
		b.WriteString(line)
		b.WriteString("\n")
		lines++
	}

	// Scroll indicator
	if len(s.Nodes) > visible {
		pct := 0
		if len(s.Nodes)-visible > 0 {
			pct = s.Offset * 100 / (len(s.Nodes) - visible)
		}
		scrollInfo := lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf(" %d/%d (%d%%)", s.Cursor+1, len(s.Nodes), pct))
		b.WriteString(scrollInfo)
	}

	return b.String()
}

func (s *Sidebar) View() string {
	if s.Width < 5 || s.Height < 3 {
		return ""
	}

	contentWidth := s.Width - 4

	var b strings.Builder

	// Header
	header := styles.TitleStyle.Render("ARIA")
	b.WriteString(header)
	b.WriteString("\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.BorderColor).Render(strings.Repeat("─", contentWidth)))
	b.WriteString("\n")

	visible := s.visibleCount()
	end := s.Offset + visible
	if end > len(s.Nodes) {
		end = len(s.Nodes)
	}

	lines := 0
	for i := s.Offset; i < end; i++ {
		node := s.Nodes[i]
		line := s.renderNode(i, node, contentWidth)
		b.WriteString(line)
		b.WriteString("\n")
		lines++
	}

	// Pad remaining
	for lines < visible {
		b.WriteString("\n")
		lines++
	}

	// Scroll indicator
	if len(s.Nodes) > visible {
		pct := 0
		if len(s.Nodes)-visible > 0 {
			pct = s.Offset * 100 / (len(s.Nodes) - visible)
		}
		scrollInfo := lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf(" %d/%d (%d%%)", s.Cursor+1, len(s.Nodes), pct))
		b.WriteString(scrollInfo)
	}

	content := b.String()

	border := styles.PaneBorder
	if s.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(s.Width - 2).Height(s.Height - 2).Render(content)
}

func (s *Sidebar) renderNode(idx int, node TreeNode, maxWidth int) string {
	selected := idx == s.Cursor

	if node.Kind == NodeSection {
		icon := "▸"
		label := fmt.Sprintf("%s %s", icon, node.Label)
		return styles.SectionTitle.Render(label)
	}

	// Indentation
	indent := strings.Repeat("  ", node.Depth)

	// Lifecycle icon
	icon := styles.LifecycleIcon(node.Status)

	// Category color dot
	catDot := categoryDot(node.Category)

	// Label
	availWidth := maxWidth - (node.Depth * 2) - 6
	label := truncate(node.Label, availWidth)

	// Prefix for agents and coding-agent group headers (both selectable,
	// Enter-able "start something new" rows).
	prefix := ""
	if node.Kind == NodeAgent || node.Kind == NodeCodingAgentGroup {
		prefix = "⊕ "
	}

	line := fmt.Sprintf("%s%s %s %s%s", indent, icon, catDot, prefix, label)

	// Add meta on the right if space allows
	if node.Meta != "" && availWidth > len(label)+len(node.Meta)+3 {
		gap := availWidth - len(label) - len(node.Meta) - len(prefix)
		if gap > 0 {
			line += strings.Repeat(" ", gap) +
				lipgloss.NewStyle().Foreground(styles.Muted).Render(node.Meta)
		}
	}

	// Disabled agents (Search Agent while paused) and the Hermes stub are
	// shown, not hidden -- re-enabling one, or Hermes eventually getting a
	// real API, shouldn't feel like the row appeared from nowhere -- but
	// dimmed throughout so they read as "not actionable right now" even
	// when the cursor is on them, unlike the normal selected-row highlight.
	if node.Status == "disabled" {
		return lipgloss.NewStyle().Foreground(styles.Muted).Italic(true).Render(line)
	}

	if selected {
		return styles.SidebarSelected.Render(line)
	}
	return styles.SidebarItem.Render(line)
}

func categoryDot(cat string) string {
	switch cat {
	case "coding":
		return lipgloss.NewStyle().Foreground(styles.Secondary).Render("●")
	case "research":
		return lipgloss.NewStyle().Foreground(styles.Accent).Render("●")
	case "infrastructure":
		return lipgloss.NewStyle().Foreground(styles.Info).Render("●")
	default:
		return lipgloss.NewStyle().Foreground(styles.Primary).Render("●")
	}
}

func relativeTime(t time.Time) string {
	if t.IsZero() {
		return ""
	}
	d := time.Since(t)
	switch {
	case d < time.Minute:
		return "now"
	case d < time.Hour:
		return fmt.Sprintf("%dm", int(d.Minutes()))
	case d < 24*time.Hour:
		return fmt.Sprintf("%dh", int(d.Hours()))
	default:
		return fmt.Sprintf("%dd", int(d.Hours()/24))
	}
}

// relativeIdle renders a shell's idle duration compactly (e.g. "now", "5m", "2h").
func relativeIdle(seconds int) string {
	switch {
	case seconds < 60:
		return "now"
	case seconds < 3600:
		return fmt.Sprintf("%dm", seconds/60)
	case seconds < 86400:
		return fmt.Sprintf("%dh", seconds/3600)
	default:
		return fmt.Sprintf("%dd", seconds/86400)
	}
}

// formatModelLabel turns a raw model id/alias into a short, human label for
// the Coding Agents sidebar headers. Pattern-matched against known model
// families rather than an exact lookup table, since pi-coding's bound model
// has already changed backends twice this cycle (chadrock -> ridge ->
// chadrockv2/agentic) -- an exhaustive map would just go stale again the
// same way the old backend-string grouping did. Extend the patterns as new
// model families get bound to these agents; unrecognized ids pass through
// as-is rather than showing nothing.
func formatModelLabel(raw string) string {
	if raw == "" {
		return ""
	}
	lower := strings.ToLower(raw)
	switch {
	case strings.Contains(lower, "35b"):
		return "Qwen3.6 35B"
	case strings.Contains(lower, "27b"):
		return "Qwen3.6 27B"
	default:
		return raw
	}
}

func truncate(s string, maxLen int) string {
	if maxLen <= 0 {
		return ""
	}
	runes := []rune(s)
	if len(runes) <= maxLen {
		return s
	}
	if maxLen <= 3 {
		return string(runes[:maxLen])
	}
	return string(runes[:maxLen-1]) + "…"
}

func max(a, b int) int {
	if a > b {
		return a
	}
	return b
}

func min(a, b int) int {
	if a < b {
		return a
	}
	return b
}
