package components

import (
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/bubbles/textarea"
	"github.com/charmbracelet/bubbles/viewport"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

type ChatView struct {
	Viewport       viewport.Model
	Input          textarea.Model
	Messages       []api.Message
	StreamBuffer   string // Partial content from active stream
	Streaming      bool
	ConversationID string
	AgentName      string
	Width          int
	Height         int
	Focused        bool
}

func NewChatView() *ChatView {
	ta := textarea.New()
	ta.Placeholder = "Type a message... (Enter to send, Alt+Enter for newline)"
	ta.CharLimit = 4096
	ta.SetHeight(3)
	ta.ShowLineNumbers = false
	ta.FocusedStyle.CursorLine = lipgloss.NewStyle()
	ta.FocusedStyle.Base = lipgloss.NewStyle()
	ta.BlurredStyle.Base = lipgloss.NewStyle()

	vp := viewport.New(80, 20)

	return &ChatView{
		Viewport: vp,
		Input:    ta,
	}
}

func (c *ChatView) SetSize(w, h int) {
	c.Width = w
	c.Height = h

	contentWidth := w - 4 // border + padding
	inputHeight := 5      // textarea + border
	vpHeight := h - inputHeight - 4

	if vpHeight < 1 {
		vpHeight = 1
	}

	c.Viewport.Width = contentWidth
	c.Viewport.Height = vpHeight
	c.Input.SetWidth(contentWidth)

	c.refreshContent()
}

func (c *ChatView) Focus() {
	c.Focused = true
	c.Input.Focus()
}

func (c *ChatView) Blur() {
	c.Focused = false
	c.Input.Blur()
}

func (c *ChatView) SetMessages(msgs []api.Message) {
	c.Messages = msgs
	c.refreshContent()
	c.Viewport.GotoBottom()
}

func (c *ChatView) AppendStreamChunk(content string) {
	c.StreamBuffer += content
	c.refreshContent()
	c.Viewport.GotoBottom()
}

func (c *ChatView) FinishStream() {
	c.StreamBuffer = ""
	c.Streaming = false
}

func (c *ChatView) Update(msg tea.Msg) (*ChatView, tea.Cmd) {
	var cmds []tea.Cmd

	if c.Focused {
		var cmd tea.Cmd
		c.Input, cmd = c.Input.Update(msg)
		cmds = append(cmds, cmd)
	}

	var cmd tea.Cmd
	c.Viewport, cmd = c.Viewport.Update(msg)
	cmds = append(cmds, cmd)

	return c, tea.Batch(cmds...)
}

func (c *ChatView) GetInput() string {
	v := c.Input.Value()
	c.Input.Reset()
	return v
}

func (c *ChatView) refreshContent() {
	if c.Width < 5 {
		return
	}

	contentWidth := c.Width - 6
	var b strings.Builder

	for _, msg := range c.Messages {
		renderMessage(&b, msg, contentWidth)
		b.WriteString("\n")
	}

	// Show streaming content
	if c.StreamBuffer != "" {
		roleTag := styles.AssistantMessage.Bold(true).Render("assistant")
		b.WriteString(roleTag + "\n")
		b.WriteString(styles.AssistantMessage.Width(contentWidth).Render(c.StreamBuffer))
		b.WriteString("\n")
	}

	c.Viewport.SetContent(b.String())
}

func renderMessage(b *strings.Builder, msg api.Message, width int) {
	switch msg.Role {
	case "user":
		roleTag := styles.UserMessage.Render("you")
		b.WriteString(roleTag + "\n")
		b.WriteString(styles.UserMessage.Width(width).UnsetBold().Render(msg.Content))
	case "assistant":
		roleTag := styles.AssistantMessage.Bold(true).Render("assistant")
		if msg.Model != "" {
			roleTag += " " + lipgloss.NewStyle().Foreground(styles.Muted).Render(fmt.Sprintf("[%s]", msg.Model))
		}
		b.WriteString(roleTag + "\n")
		b.WriteString(styles.AssistantMessage.Width(width).Render(msg.Content))
	case "system":
		b.WriteString(styles.SystemMessage.Width(width).Render("system: " + msg.Content))
	case "tool":
		b.WriteString(styles.ToolMessage.Width(width).Render("⚙ " + msg.Content))
	default:
		b.WriteString(msg.Content)
	}
}

func (c *ChatView) View() string {
	if c.Width < 5 || c.Height < 5 {
		return ""
	}

	// Header
	header := ""
	if c.AgentName != "" {
		header = styles.TitleStyle.Render(c.AgentName)
	} else if c.ConversationID != "" {
		header = styles.TitleStyle.Render("Chat")
	} else {
		header = styles.TitleStyle.Render("Select a conversation or agent →")
	}

	// Build chat content
	vpView := c.Viewport.View()

	// Input area
	inputView := c.Input.View()

	// Streaming indicator
	streamIndicator := ""
	if c.Streaming {
		streamIndicator = styles.SpinnerStyle.Render(" ● streaming...")
	}

	content := lipgloss.JoinVertical(lipgloss.Left,
		header,
		streamIndicator,
		vpView,
		"",
		inputView,
	)

	border := styles.PaneBorder
	if c.Focused {
		border = styles.PaneBorderActive
	}

	return border.Width(c.Width - 2).Height(c.Height - 2).Render(content)
}
