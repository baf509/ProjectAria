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
func (s *Sidebar) SetData(agents []api.Agent, convs []api.Conversation, sessions []api.CodingSession) {
	s.Nodes = nil

	// Build agent lookup
	agentByID := make(map[string]api.Agent)
	for _, a := range agents {
		agentByID[a.ID] = a
	}

	// --- Agents section (exclude default/ARIA — she's the coordinator, not a delegated agent) ---
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
		}
	}

	// --- Active Coding Sessions section ---
	activeSessions := filterSessions(sessions, "running")
	if len(activeSessions) > 0 {
		s.Nodes = append(s.Nodes, TreeNode{
			Kind:     NodeSection,
			Label:    fmt.Sprintf("Coding Sessions (%d)", len(activeSessions)),
			Children: len(activeSessions),
		})
		for i := range activeSessions {
			cs := &activeSessions[i]
			label := truncate(cs.Prompt, 30)
			if label == "" {
				label = cs.ID[:8]
			}
			s.Nodes = append(s.Nodes, TreeNode{
				ID:            cs.ID,
				Label:         label,
				Kind:          NodeCodingSession,
				Category:      "coding",
				Status:        cs.Status,
				Meta:          cs.Backend,
				Depth:         1,
				CodingSession: cs,
			})
		}
	}

	// --- Conversations section ---
	if len(convs) > 0 {
		s.Nodes = append(s.Nodes, TreeNode{
			Kind:     NodeSection,
			Label:    fmt.Sprintf("Conversations (%d)", len(convs)),
			Children: len(convs),
		})
		for i := range convs {
			c := &convs[i]
			category := "chat"
			agentSlug := ""
			if a, ok := agentByID[c.AgentID]; ok {
				category = a.ModeCategory
				agentSlug = a.Slug
			}
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

			s.Nodes = append(s.Nodes, TreeNode{
				ID:           c.ID,
				Label:        title,
				Kind:         NodeConversation,
				Category:     category,
				AgentSlug:    agentSlug,
				Status:       c.Status,
				Meta:         relativeTime(c.UpdatedAt),
				Depth:        1,
				Conversation: c,
			})
		}
	}

	// Ensure cursor is on a selectable node
	s.fixCursor()
}

// Legacy compat
func (s *Sidebar) SetConversations(convs []api.Conversation, agents []api.Agent) {
	s.SetData(agents, convs, nil)
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

func filterSessions(sessions []api.CodingSession, status string) []api.CodingSession {
	var out []api.CodingSession
	for i := range sessions {
		if sessions[i].Status == status {
			out = append(out, sessions[i])
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
