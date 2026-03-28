package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// ToolsBrowser displays registered tools and MCP servers.
type ToolsBrowser struct {
	Viewport   viewport.Model
	Width      int
	Height     int
	Focused    bool
	Tools      []api.Tool
	MCPServers []api.MCPServer
}

func NewToolsBrowser() *ToolsBrowser {
	vp := viewport.New(80, 20)
	return &ToolsBrowser{Viewport: vp}
}

func (tb *ToolsBrowser) SetSize(w, h int) {
	tb.Width = w
	tb.Height = h
	tb.Viewport.Width = w - 4
	tb.Viewport.Height = h - 6
	if tb.Viewport.Height < 1 {
		tb.Viewport.Height = 1
	}
}

func (tb *ToolsBrowser) Focus() { tb.Focused = true }
func (tb *ToolsBrowser) Blur()  { tb.Focused = false }

func (tb *ToolsBrowser) SetData(tools []api.Tool, servers []api.MCPServer) {
	tb.Tools = tools
	tb.MCPServers = servers
	tb.refreshContent()
}

func (tb *ToolsBrowser) Update(msg tea.Msg) (*ToolsBrowser, tea.Cmd) {
	var cmd tea.Cmd
	tb.Viewport, cmd = tb.Viewport.Update(msg)
	return tb, cmd
}

func (tb *ToolsBrowser) refreshContent() {
	if tb.Width < 10 {
		return
	}

	cw := tb.Width - 8
	var b strings.Builder

	// ---- Built-in Tools ----
	var builtins, mcpTools []api.Tool
	for _, t := range tb.Tools {
		if t.Type == "mcp" {
			mcpTools = append(mcpTools, t)
		} else {
			builtins = append(builtins, t)
		}
	}

	b.WriteString(styles.SectionTitle.Render(fmt.Sprintf("  Built-in Tools (%d)", len(builtins))))
	b.WriteString("\n\n")

	if len(builtins) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  No built-in tools registered\n"))
	} else {
		for _, t := range builtins {
			icon := lipgloss.NewStyle().Foreground(styles.Secondary).Render("⚙")
			name := lipgloss.NewStyle().Foreground(styles.Text).Bold(true).Render(t.Name)
			desc := ""
			if t.Description != "" {
				desc = lipgloss.NewStyle().Foreground(styles.SubText).Render(
					" — " + truncate(t.Description, cw-len(t.Name)-8))
			}
			b.WriteString(fmt.Sprintf("  %s %s%s\n", icon, name, desc))
		}
	}

	// ---- MCP Servers ----
	if len(tb.MCPServers) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render(fmt.Sprintf("  MCP Servers (%d)", len(tb.MCPServers))))
		b.WriteString("\n\n")

		for _, s := range tb.MCPServers {
			icon := styles.LifecycleIcon(s.Status)
			name := lipgloss.NewStyle().Foreground(styles.Text).Bold(true).Render(s.Name)
			tools := lipgloss.NewStyle().Foreground(styles.Muted).Render(
				fmt.Sprintf("(%d tools)", s.Tools))
			b.WriteString(fmt.Sprintf("  %s %s %s\n", icon, name, tools))
		}
	}

	// ---- MCP Tools ----
	if len(mcpTools) > 0 {
		b.WriteString("\n")
		b.WriteString(styles.SectionTitle.Render(fmt.Sprintf("  MCP Tools (%d)", len(mcpTools))))
		b.WriteString("\n\n")

		for _, t := range mcpTools {
			icon := lipgloss.NewStyle().Foreground(styles.Info).Render("⚡")
			name := lipgloss.NewStyle().Foreground(styles.Text).Render(t.Name)
			server := ""
			if t.Server != "" {
				server = lipgloss.NewStyle().Foreground(styles.Muted).Render(" [" + t.Server + "]")
			}
			desc := ""
			if t.Description != "" {
				desc = lipgloss.NewStyle().Foreground(styles.SubText).Render(
					"\n      " + truncate(t.Description, cw-6))
			}
			b.WriteString(fmt.Sprintf("  %s %s%s%s\n", icon, name, server, desc))
		}
	}

	tb.Viewport.SetContent(b.String())
}

func (tb *ToolsBrowser) View() string {
	if tb.Width < 10 || tb.Height < 5 {
		return ""
	}

	total := len(tb.Tools)
	header := styles.TitleStyle.Render(fmt.Sprintf("Tools & MCP (%d)", total))
	vpView := tb.Viewport.View()
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(
		"  r: refresh │ Esc: back")

	content := lipgloss.JoinVertical(lipgloss.Left, header, "", vpView, footer)

	border := styles.PaneBorder
	if tb.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(tb.Width - 2).Height(tb.Height - 2).Render(content)
}
