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
	NodeSection TreeNodeKind = iota // Collapsible section header
	NodeAgent                       // Agent template (start new conversation)
	NodeConversation                // Existing conversation
	NodeCodingSession               // Active coding session
	NodeShell                       // Watched tmux shell (the fleet ARIA observes)
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

	// Original data pointers for detail views
	Agent          *api.Agent
	Conversation   *api.Conversation
	CodingSession  *api.CodingSession
	Shell          *api.Shell
}

type Sidebar struct {
	Nodes    []TreeNode
	Cursor   int
	Offset   int
	Height   int
	Width    int
	Focused  bool
	Filter   string // future: text filter
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
// Structure (2026-07-30 restructuring — Agents/Shells/CodingSessions/
// Conversations used to be four flat, overlapping lists; a coding session
// and its backing shell showed up as two separate-looking rows for the same
// live process, and an agent's conversations had no visible link to the
// agent itself):
//
//	▸ Agents (N)                     -- each agent, its conversations nested under it
//	▸ Pool (x/1 active)              -- chadrock: single-consumer, queue rather than stack
//	▸ Ridge (x/1 active)             -- NInfer: same single-consumer constraint
//	▸ Claude Code (N active)         -- cloud, unbounded concurrent sessions
//	▸ Codex (N active)               -- cloud, unbounded (only shown if any exist)
//	▸ Your Shells (N)                -- hand-run shells with no coding_sessions record
//	▸ Other Conversations (N)        -- conversations whose agent no longer exists
//
// A coding session and the shell backing it are the same live process as of
// 2026-07-30 (pi-code runs on the shell substrate too) -- rendered as ONE
// row per session, not two; "Your Shells" explicitly excludes anything a
// coding session already claims.
func (s *Sidebar) SetData(agents []api.Agent, convs []api.Conversation, sessions []api.CodingSession, shells []api.Shell) {
	// Remember what was selected so the 3s refresh doesn't yank the cursor onto
	// a different row (which would make Enter open the wrong thing).
	var prevID string
	var prevKind TreeNodeKind
	if s.Cursor >= 0 && s.Cursor < len(s.Nodes) {
		prevID = s.Nodes[s.Cursor].ID
		prevKind = s.Nodes[s.Cursor].Kind
	}

	s.Nodes = nil

	// Build agent lookup
	agentByID := make(map[string]api.Agent)
	for _, a := range agents {
		agentByID[a.ID] = a
	}

	// Conversations grouped by owning agent (nil-keyed slice for orphans).
	convsByAgent := make(map[string][]*api.Conversation)
	var orphanConvs []*api.Conversation
	for i := range convs {
		c := &convs[i]
		if _, ok := agentByID[c.AgentID]; ok {
			convsByAgent[c.AgentID] = append(convsByAgent[c.AgentID], c)
		} else {
			orphanConvs = append(orphanConvs, c)
		}
	}

	// --- Agents section (exclude default/ARIA — she's the coordinator, not a
	// delegated agent), each with its own conversations nested beneath it. ---
	var delegatedAgents []*api.Agent
	for i := range agents {
		if !agents[i].IsDefault {
			delegatedAgents = append(delegatedAgents, &agents[i])
		}
	}
	if len(delegatedAgents) > 0 {
		s.Nodes = append(s.Nodes, TreeNode{
			Kind:     NodeSection,
			Label:    fmt.Sprintf("Agents (%d)", len(delegatedAgents)),
			Children: len(delegatedAgents),
		})
		for _, a := range delegatedAgents {
			s.Nodes = append(s.Nodes, TreeNode{
				ID:        a.Slug,
				Label:     a.Name,
				Kind:      NodeAgent,
				Category:  a.ModeCategory,
				AgentSlug: a.Slug,
				Meta:      fmt.Sprintf("%s/%s", a.LLM.Backend, a.LLM.Model),
				Status:    "idle",
				Depth:     1,
				Agent:     a,
			})
			for _, c := range convsByAgent[a.ID] {
				s.Nodes = append(s.Nodes, conversationNode(c, a.ModeCategory, a.Slug, 2))
			}
		}
	}

	// Shell lookup by name, and the set already claimed by a coding session --
	// a session and its shell are one live process, shown once.
	shellByName := make(map[string]*api.Shell)
	for i := range shells {
		shellByName[shells[i].Name] = &shells[i]
	}
	claimedShells := make(map[string]bool)

	// --- Coding-session backend groups. Pool/Ridge have a real single-
	// consumer ceiling at the model-server level (see
	// coding_max_concurrent_{laguna,ridge}_sessions server-side) -- shown as
	// "x/1 active" so hitting the limit is visible, not queued sessions
	// silently piling up looking like a bug. Claude Code/Codex are cloud,
	// unbounded. ---
	active := filterSessionsAny(sessions, "running", "queued")
	var poolSessions, ridgeSessions, claudeSessions, codexSessions, otherSessions []*api.CodingSession
	for i := range active {
		cs := active[i]
		switch {
		case cs.Backend == "pool":
			poolSessions = append(poolSessions, cs)
		case cs.Backend == "pi-code" && cs.LLM == "ridge":
			ridgeSessions = append(ridgeSessions, cs)
		case cs.Backend == "claude_code":
			claudeSessions = append(claudeSessions, cs)
		case cs.Backend == "codex":
			codexSessions = append(codexSessions, cs)
		default:
			otherSessions = append(otherSessions, cs)
		}
		if cs.ShellName != "" {
			claimedShells[cs.ShellName] = true
		}
	}

	s.appendBackendGroup("Pool", poolSessions, 1, shellByName)
	s.appendBackendGroup("Ridge", ridgeSessions, 1, shellByName)
	s.appendBackendGroup("Claude Code", claudeSessions, 0, shellByName)
	s.appendBackendGroup("Codex", codexSessions, 0, shellByName)
	s.appendBackendGroup("Local Pi-Code", otherSessions, 0, shellByName)

	// --- Your Shells: hand-run shells only (no coding_sessions record) --
	// the fleet ARIA observes but didn't spawn. Distinct from the backend
	// groups above, which already cover every ARIA-spawned session's shell. ---
	var handRun []*api.Shell
	for i := range shells {
		if !claimedShells[shells[i].Name] {
			handRun = append(handRun, &shells[i])
		}
	}
	if len(handRun) > 0 {
		awaiting, done := 0, 0
		for _, sh := range handRun {
			if sh.AwaitingInput {
				awaiting++
			}
			if sh.ActivityState == "done" {
				done++
			}
		}
		label := fmt.Sprintf("Your Shells (%d)", len(handRun))
		switch {
		case awaiting > 0 && done > 0:
			label = fmt.Sprintf("Your Shells (%d · %d awaiting · %d done)", len(handRun), awaiting, done)
		case awaiting > 0:
			label = fmt.Sprintf("Your Shells (%d · %d awaiting)", len(handRun), awaiting)
		case done > 0:
			label = fmt.Sprintf("Your Shells (%d · %d done)", len(handRun), done)
		}
		s.Nodes = append(s.Nodes, TreeNode{Kind: NodeSection, Label: label, Children: len(handRun)})
		for _, sh := range handRun {
			s.Nodes = append(s.Nodes, shellNode(sh, 1))
		}
	}

	// --- Other Conversations: agent no longer exists (deleted/renamed). Not
	// dropped silently -- surfaced so nothing just vanishes. ---
	if len(orphanConvs) > 0 {
		s.Nodes = append(s.Nodes, TreeNode{
			Kind:     NodeSection,
			Label:    fmt.Sprintf("Other Conversations (%d)", len(orphanConvs)),
			Children: len(orphanConvs),
		})
		for _, c := range orphanConvs {
			s.Nodes = append(s.Nodes, conversationNode(c, "chat", "", 1))
		}
	}

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

// appendBackendGroup renders one coding-session backend family as a section:
// "Name (x/limit active)" when limit > 0 (Pool/Ridge's real single-consumer
// ceiling), or "Name (N active)" when limit == 0 (unbounded, e.g. Claude
// Code/Codex). Queued sessions count toward the header but are labeled
// "queued", not "active", so a hit limit reads as "waiting," not "broken."
// Each session renders as ONE row using its live shell's activity_state when
// it has a shell (every backend does, as of the 2026-07-30 pi-code change) --
// not a separate, possibly-stale coding_sessions.Status.
func (s *Sidebar) appendBackendGroup(name string, sessions []*api.CodingSession, limit int, shellByName map[string]*api.Shell) {
	if len(sessions) == 0 {
		return
	}
	activeCount, queuedCount := 0, 0
	for _, cs := range sessions {
		if cs.Status == "queued" {
			queuedCount++
		} else {
			activeCount++
		}
	}
	var label string
	if limit > 0 {
		label = fmt.Sprintf("%s (%d/%d active", name, activeCount, limit)
	} else {
		label = fmt.Sprintf("%s (%d active", name, activeCount)
	}
	if queuedCount > 0 {
		label += fmt.Sprintf(" · %d queued", queuedCount)
	}
	label += ")"
	s.Nodes = append(s.Nodes, TreeNode{Kind: NodeSection, Label: label, Children: len(sessions)})
	for _, cs := range sessions {
		s.Nodes = append(s.Nodes, codingSessionNode(cs, shellByName[cs.ShellName], 1))
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

func shellNode(sh *api.Shell, depth int) TreeNode {
	status := sh.Status
	meta := relativeIdle(sh.IdleSeconds)
	switch sh.ActivityState {
	case "blocked":
		status = "blocked"
		meta = "⏳ blocked"
	case "done":
		status = "done"
		meta = "✓ done"
	}
	name := sh.ShortName
	if name == "" {
		name = sh.Name
	}
	return TreeNode{
		ID:       sh.Name,
		Label:    name,
		Kind:     NodeShell,
		Category: "coding",
		Status:   status,
		Meta:     meta,
		Depth:    depth,
		Shell:    sh,
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

	// Prefix for agents
	prefix := ""
	if node.Kind == NodeAgent {
		prefix = "⊕ "
	} else if node.Kind == NodeShell {
		prefix = "❯ "
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

// filterSessionsAny returns pointers into `sessions` whose Status matches any
// of `statuses` -- pointers (not copies) so callers can key off ShellName etc.
// without the slice-of-structs aliasing footgun copying would introduce.
func filterSessionsAny(sessions []api.CodingSession, statuses ...string) []*api.CodingSession {
	var out []*api.CodingSession
	for i := range sessions {
		for _, want := range statuses {
			if sessions[i].Status == want {
				out = append(out, &sessions[i])
				break
			}
		}
	}
	return out
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
