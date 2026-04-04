package ui

import (
	"fmt"
	"strings"
	"time"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/components"
	"github.com/ben/aria-tui/internal/ui/styles"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// ---- Screens ----

type screen int

const (
	screenDashboard screen = iota
	screenChat
	screenSession
	screenMemory
	screenUsage
	screenTools
	screenObservations
	screenDB
)

// Which quadrant has focus on the dashboard
type quadrant int

const (
	quadTopLeft  quadrant = iota // Task tree
	quadTopRight                 // Detail / Log
	quadBotLeft                  // Tools menu
	quadBotRight                 // Vitals
)

// ---- Tea Messages ----

type dashboardTick struct{}
type snapshotLoaded struct{ snap *api.DashboardSnapshot }
type conversationsLoaded struct {
	convs  []api.Conversation
	agents []api.Agent
}
type conversationOpened struct{ conv *api.ConversationDetail }
type conversationCreated struct{ conv *api.ConversationDetail }
type conversationDeleted struct{ id string }
type streamStartMsg struct{ ch <-chan api.StreamChunk }
type streamChunkMsg struct{ chunk api.StreamChunk }
type streamDoneMsg struct{}
type codingOutputLoaded struct {
	sessionID string
	output    string
}
type memoriesLoaded struct{ memories []api.Memory }
type usageDataLoaded struct {
	summary *api.UsageSummary
	byAgent []api.AgentUsage
	byModel []api.ModelUsage
	llm     []api.LLMBackendStatus
}
type toolsLoaded struct {
	tools   []api.Tool
	servers []api.MCPServer
}
type observationsLoaded struct{ obs []api.Observation }
type collectionsLoaded struct{ cols []api.CollectionInfo }
type queryResultLoaded struct{ result *api.QueryResult }
type documentLoaded struct{ doc map[string]interface{} }
type errMsg struct{ err error }

// ---- Main Model ----

type Model struct {
	client  *api.Client
	sidebar *components.Sidebar
	chat    *components.ChatView
	session *components.SessionView
	vitals  *components.VitalsPanel
	menu    *components.ToolsMenu

	// Sub-screens
	memBrowser   *components.MemoryBrowser
	usageMonitor *components.UsageMonitor
	toolsBrowser *components.ToolsBrowser
	obsView      *components.ObservationsView
	dbBrowser    *components.DBBrowser

	// Navigation
	screen     screen
	prevScreen screen
	quad       quadrant // active quadrant on dashboard
	width      int
	height     int
	ready      bool

	// Layout dimensions (computed)
	leftW, rightW int
	topH, botH    int
	headerH       int

	// State
	activeConvID    string
	activeSessionID string
	agents          []api.Agent
	snapshot        *api.DashboardSnapshot
	streamCh        <-chan api.StreamChunk

	// Detail panel content (for dashboard top-right)
	detailText string
	logText    string
}

func NewModel(client *api.Client) Model {
	return Model{
		client:       client,
		sidebar:      components.NewSidebar(),
		chat:         components.NewChatView(),
		session:      components.NewSessionView(),
		vitals:       components.NewVitalsPanel(),
		menu:         components.NewToolsMenu(),
		memBrowser:   components.NewMemoryBrowser(),
		usageMonitor: components.NewUsageMonitor(),
		toolsBrowser: components.NewToolsBrowser(),
		obsView:      components.NewObservationsView(),
		dbBrowser:    components.NewDBBrowser(),
		screen:       screenDashboard,
		quad:         quadTopLeft,
		headerH:      1,
	}
}

func (m Model) Init() tea.Cmd {
	return tea.Batch(
		fetchSnapshot(m.client),
		tickCmd(),
	)
}

// ---- Update ----

func (m Model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	var cmds []tea.Cmd

	switch msg := msg.(type) {
	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height
		m.ready = true
		m.layout()
		return m, nil

	case tea.KeyMsg:
		cmd := m.handleKey(msg)
		if cmd != nil {
			cmds = append(cmds, cmd)
		}

	case dashboardTick:
		cmds = append(cmds, fetchSnapshot(m.client), tickCmd())

	case snapshotLoaded:
		m.snapshot = msg.snap
		if msg.snap != nil {
			m.agents = msg.snap.Agents
			m.sidebar.SetData(msg.snap.Agents, msg.snap.Conversations, msg.snap.CodingSessions)
			m.vitals.Update(msg.snap)
		}

	case conversationsLoaded:
		m.agents = msg.agents
		m.sidebar.SetConversations(msg.convs, msg.agents)

	case conversationOpened:
		m.activeConvID = msg.conv.ID
		m.chat.ConversationID = msg.conv.ID
		m.chat.SetMessages(msg.conv.Messages)
		m.chat.Streaming = false
		m.chat.AgentName = ""
		for _, a := range m.agents {
			if a.ID == msg.conv.AgentID {
				m.chat.AgentName = a.Name
				break
			}
		}
		m.pushScreen(screenChat)

	case conversationCreated:
		m.activeConvID = msg.conv.ID
		m.chat.ConversationID = msg.conv.ID
		m.chat.SetMessages(msg.conv.Messages)
		m.chat.AgentName = ""
		for _, a := range m.agents {
			if a.ID == msg.conv.AgentID {
				m.chat.AgentName = a.Name
				break
			}
		}
		m.pushScreen(screenChat)
		cmds = append(cmds, fetchSnapshot(m.client))

	case conversationDeleted:
		if m.activeConvID == msg.id {
			m.activeConvID = ""
			m.popScreen()
		}
		cmds = append(cmds, fetchSnapshot(m.client))

	case streamStartMsg:
		m.streamCh = msg.ch
		cmds = append(cmds, waitForChunk(m.streamCh))

	case streamChunkMsg:
		switch msg.chunk.Type {
		case "text":
			m.chat.AppendStreamChunk(msg.chunk.Content)
			if m.streamCh != nil {
				cmds = append(cmds, waitForChunk(m.streamCh))
			}
		case "done":
			m.chat.FinishStream()
			m.streamCh = nil
			cmds = append(cmds, openConversation(m.client, m.activeConvID))
		case "error":
			m.chat.AppendStreamChunk("\n[Error: " + msg.chunk.Error + "]")
			m.chat.FinishStream()
			m.streamCh = nil
		default:
			if m.streamCh != nil {
				cmds = append(cmds, waitForChunk(m.streamCh))
			}
		}

	case streamDoneMsg:
		m.chat.FinishStream()
		m.streamCh = nil

	case codingOutputLoaded:
		m.logText = msg.output
		if m.activeSessionID == msg.sessionID && m.screen == screenSession {
			m.session.SetOutput(msg.output)
		}

	case memoriesLoaded:
		m.memBrowser.SetMemories(msg.memories)

	case usageDataLoaded:
		m.usageMonitor.SetData(msg.summary, msg.byAgent, msg.byModel, msg.llm)

	case toolsLoaded:
		m.toolsBrowser.SetData(msg.tools, msg.servers)

	case observationsLoaded:
		m.obsView.SetData(msg.obs)

	case collectionsLoaded:
		m.dbBrowser.SetCollections(msg.cols)

	case queryResultLoaded:
		m.dbBrowser.SetQueryResult(msg.result)

	case documentLoaded:
		m.dbBrowser.SetDocument(msg.doc)

	case errMsg:
		if m.screen == screenChat {
			m.chat.AppendStreamChunk("\n[Error: " + msg.err.Error() + "]")
			m.chat.FinishStream()
		}
	}

	// Update child components on sub-screens
	if m.screen == screenChat {
		var cmd tea.Cmd
		m.chat, cmd = m.chat.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenSession {
		var cmd tea.Cmd
		m.session, cmd = m.session.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenMemory {
		var cmd tea.Cmd
		m.memBrowser, cmd = m.memBrowser.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenUsage {
		var cmd tea.Cmd
		m.usageMonitor, cmd = m.usageMonitor.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenTools {
		var cmd tea.Cmd
		m.toolsBrowser, cmd = m.toolsBrowser.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenObservations {
		var cmd tea.Cmd
		m.obsView, cmd = m.obsView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenDB {
		var cmd tea.Cmd
		m.dbBrowser, cmd = m.dbBrowser.Update(msg)
		cmds = append(cmds, cmd)
	}

	return m, tea.Batch(cmds...)
}

// ---- Key Handling ----

func (m *Model) handleKey(msg tea.KeyMsg) tea.Cmd {
	key := msg.String()

	switch key {
	case "ctrl+c":
		return tea.Quit
	}

	if m.screen != screenDashboard {
		return m.handleSubScreenKey(msg)
	}
	return m.handleDashboardKey(key)
}

func (m *Model) handleDashboardKey(key string) tea.Cmd {
	switch key {
	case "q":
		return tea.Quit
	case "tab":
		m.quad = (m.quad + 1) % 4
	case "shift+tab":
		m.quad = (m.quad + 3) % 4 // backwards

	// Sidebar navigation (always active)
	case "up", "k":
		m.sidebar.Up()
		m.updateDetail()
	case "down", "j":
		m.sidebar.Down()
		m.updateDetail()
	case "enter":
		node := m.sidebar.Selected()
		if node == nil {
			return nil
		}
		switch node.Kind {
		case components.NodeAgent:
			return createConversation(m.client, node.ID, "")
		case components.NodeConversation:
			return openConversation(m.client, node.ID)
		case components.NodeCodingSession:
			m.activeSessionID = node.ID
			m.session.SetSession(node.CodingSession)
			m.pushScreen(screenSession)
			return loadCodingOutput(m.client, node.ID)
		}

	// Quick-nav hotkeys
	case "c":
		slug := ""
		for _, a := range m.agents {
			if a.IsDefault {
				slug = a.Slug
				break
			}
		}
		return createConversation(m.client, slug, "")
	case "n":
		return createConversation(m.client, "", "")
	case "p":
		return createPrivateConversation(m.client)
	case "m":
		m.pushScreen(screenMemory)
		return loadMemories(m.client, "", 50)
	case "u":
		m.pushScreen(screenUsage)
		return loadUsageData(m.client)
	case "t":
		m.pushScreen(screenTools)
		return loadTools(m.client)
	case "o":
		m.pushScreen(screenObservations)
		return loadObservations(m.client, 50)
	case "b":
		m.pushScreen(screenDB)
		return loadCollections(m.client)
	case "d":
		node := m.sidebar.Selected()
		if node != nil && node.Kind == components.NodeConversation {
			return deleteConversation(m.client, node.ID)
		}
	case "r":
		return fetchSnapshot(m.client)
	}
	return nil
}

func (m *Model) handleSubScreenKey(msg tea.KeyMsg) tea.Cmd {
	key := msg.String()

	switch key {
	case "esc":
		m.popScreen()
		return nil
	}

	switch m.screen {
	case screenChat:
		if key == "enter" {
			if m.chat.Streaming {
				return nil
			}
			input := m.chat.GetInput()
			if input == "" {
				return nil
			}
			m.chat.Messages = append(m.chat.Messages, api.Message{Role: "user", Content: input})
			m.chat.StreamBuffer = ""
			m.chat.Streaming = true
			m.chat.SetMessages(m.chat.Messages)
			return sendMessage(m.client, m.activeConvID, input)
		}
	case screenSession:
		switch key {
		case "enter":
			input := m.session.GetInput()
			if input != "" {
				return sendCodingInput(m.client, m.activeSessionID, input)
			}
		case "s":
			if m.activeSessionID != "" {
				return stopCodingSession(m.client, m.activeSessionID)
			}
		case "r":
			if m.activeSessionID != "" {
				return loadCodingOutput(m.client, m.activeSessionID)
			}
		}
	case screenMemory:
		if key == "enter" {
			query := m.memBrowser.GetQuery()
			if query == "" {
				return loadMemories(m.client, "", 50)
			}
			return loadMemories(m.client, query, 20)
		}
	case screenUsage, screenTools, screenObservations:
		if key == "r" {
			switch m.screen {
			case screenUsage:
				return loadUsageData(m.client)
			case screenTools:
				return loadTools(m.client)
			case screenObservations:
				return loadObservations(m.client, 50)
			}
		}
	case screenDB:
		if m.dbBrowser.IsEditing() {
			switch key {
			case "esc":
				m.dbBrowser.ToggleEditing()
			case "enter":
				m.dbBrowser.ToggleEditing()
				col := m.dbBrowser.CurrentCollection()
				if col != "" {
					m.dbBrowser.SetPage(0)
					return queryCollection(m.client, col, 20, 0, m.dbBrowser.GetFilter())
				}
			default:
				m.dbBrowser.HandleFilterKey(key)
			}
			return nil
		}
		switch key {
		case "up", "k":
			m.dbBrowser.Up()
		case "down", "j":
			m.dbBrowser.Down()
		case "enter":
			switch m.dbBrowser.Mode() {
			case 0: // collections
				col := m.dbBrowser.SelectedCollection()
				if col != "" {
					m.dbBrowser.SetCollection(col)
					m.dbBrowser.SetPage(0)
					m.dbBrowser.SetFilter("")
					return queryCollection(m.client, col, 20, 0, "")
				}
			case 1: // documents
				docID := m.dbBrowser.SelectedDocID()
				col := m.dbBrowser.CurrentCollection()
				if docID != "" && col != "" {
					return loadDocument(m.client, col, docID)
				}
			}
		case "backspace":
			if !m.dbBrowser.GoBack() {
				m.popScreen()
			}
		case "/":
			if m.dbBrowser.Mode() == 1 { // documents mode
				m.dbBrowser.ToggleEditing()
			}
		case "n":
			if m.dbBrowser.Mode() == 1 { // next page
				col := m.dbBrowser.CurrentCollection()
				p := m.dbBrowser.Page() + 1
				m.dbBrowser.SetPage(p)
				return queryCollection(m.client, col, 20, p*20, m.dbBrowser.GetFilter())
			}
		case "p":
			if m.dbBrowser.Mode() == 1 && m.dbBrowser.Page() > 0 { // prev page
				col := m.dbBrowser.CurrentCollection()
				p := m.dbBrowser.Page() - 1
				m.dbBrowser.SetPage(p)
				return queryCollection(m.client, col, 20, p*20, m.dbBrowser.GetFilter())
			}
		case "ctrl+u":
			for i := 0; i < 10; i++ {
				m.dbBrowser.ScrollUp()
			}
		case "ctrl+d":
			for i := 0; i < 10; i++ {
				m.dbBrowser.ScrollDown()
			}
		}
	}
	return nil
}

// ---- Screen Stack ----

func (m *Model) pushScreen(s screen) {
	m.prevScreen = m.screen
	m.screen = s
	if s == screenChat {
		m.chat.Focus()
	} else if s == screenSession {
		m.session.Focus()
	} else if s == screenMemory {
		m.memBrowser.Focus()
	} else if s == screenDB {
		m.dbBrowser.Focus()
	}
}

func (m *Model) popScreen() {
	m.chat.Blur()
	m.session.Blur()
	m.memBrowser.Blur()
	m.dbBrowser.Blur()
	m.screen = m.prevScreen
	m.prevScreen = screenDashboard
}

// ---- Detail Panel ----

func (m *Model) updateDetail() {
	node := m.sidebar.Selected()
	if node == nil {
		m.detailText = ""
		return
	}

	var b strings.Builder
	switch node.Kind {
	case components.NodeAgent:
		a := node.Agent
		if a != nil {
			b.WriteString(styles.PanelTitle.Render("Agent: "+a.Name) + "\n")
			b.WriteString(styles.VitalLabel.Render("  Slug: ") + a.Slug + "\n")
			b.WriteString(styles.VitalLabel.Render("  Backend: ") + a.LLM.Backend + "/" + a.LLM.Model + "\n")
			b.WriteString(styles.VitalLabel.Render("  Category: ") + a.ModeCategory + "\n")
			if a.Description != "" {
				b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.SubText).Render("  "+a.Description) + "\n")
			}
			b.WriteString("\n" + styles.HelpKey.Render("  Enter") + styles.HelpDesc.Render(" start conversation"))
		}
	case components.NodeConversation:
		c := node.Conversation
		if c != nil {
			b.WriteString(styles.PanelTitle.Render("Conversation") + "\n")
			b.WriteString(styles.VitalLabel.Render("  Title: ") + styles.VitalValue.Render(c.Title) + "\n")
			b.WriteString(styles.VitalLabel.Render("  Status: ") + styles.LifecycleIcon(c.Status) + " " + c.Status + "\n")
			b.WriteString(styles.VitalLabel.Render("  Messages: ") + fmt.Sprintf("%d", c.Stats.MessageCount) + "\n")
			b.WriteString(styles.VitalLabel.Render("  Tokens: ") + fmt.Sprintf("%d", c.Stats.TotalTokens) + "\n")
			if len(c.Tags) > 0 {
				b.WriteString(styles.VitalLabel.Render("  Tags: ") + strings.Join(c.Tags, ", ") + "\n")
			}
			b.WriteString("\n" + styles.HelpKey.Render("  Enter") + styles.HelpDesc.Render(" open  ") +
				styles.HelpKey.Render("d") + styles.HelpDesc.Render(" delete"))
		}
	case components.NodeCodingSession:
		cs := node.CodingSession
		if cs != nil {
			b.WriteString(styles.PanelTitle.Render("Coding Session") + "\n")
			b.WriteString(styles.VitalLabel.Render("  Status: ") + styles.LifecycleIcon(cs.Status) + " " + cs.Status + "\n")
			b.WriteString(styles.VitalLabel.Render("  Backend: ") + cs.Backend + "\n")
			if cs.Model != "" {
				b.WriteString(styles.VitalLabel.Render("  Model: ") + cs.Model + "\n")
			}
			if cs.Workspace != "" {
				b.WriteString(styles.VitalLabel.Render("  Workspace: ") + cs.Workspace + "\n")
			}
			if cs.Branch != "" {
				b.WriteString(styles.VitalLabel.Render("  Branch: ") + cs.Branch + "\n")
			}
			if cs.Prompt != "" {
				b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.SubText).Italic(true).Render("  ❯ "+cs.Prompt) + "\n")
			}
			b.WriteString("\n" + styles.HelpKey.Render("  Enter") + styles.HelpDesc.Render(" open"))
		}
	}
	m.detailText = b.String()
}

// ---- Layout ----

func (m *Model) layout() {
	if m.width == 0 || m.height == 0 {
		return
	}

	m.headerH = 1
	footerH := 1
	bodyH := m.height - m.headerH - footerH

	// ABP ratio: 1:2 columns, 2:1 rows
	m.leftW = m.width / 3
	if m.leftW < 20 {
		m.leftW = 20
	}
	m.rightW = m.width - m.leftW

	m.topH = (bodyH * 2) / 3
	m.botH = bodyH - m.topH

	// Size child components for sub-screens
	m.chat.SetSize(m.width, bodyH)
	m.session.SetSize(m.width, bodyH)
	m.memBrowser.SetSize(m.width, bodyH)
	m.usageMonitor.SetSize(m.width, bodyH)
	m.toolsBrowser.SetSize(m.width, bodyH)
	m.obsView.SetSize(m.width, bodyH)
	m.dbBrowser.SetSize(m.width, bodyH)

	m.sidebar.SetSize(m.leftW, m.topH)
	m.vitals.SetSize(m.rightW, m.botH)
}

// ---- View ----

func (m Model) View() string {
	if !m.ready {
		return styles.HeaderStyle.Width(m.width).Render("  ARIA") + "\n\n  Loading..."
	}

	header := m.renderHeader()
	footer := m.renderFooter()

	var body string
	if m.screen != screenDashboard {
		body = m.renderSubScreen()
	} else {
		body = m.renderDashboard()
	}

	return lipgloss.JoinVertical(lipgloss.Left, header, body, footer)
}

func (m Model) renderHeader() string {
	left := "  ARIA"

	right := ""
	if m.snapshot != nil {
		healthy := m.snapshot.Health != nil && m.snapshot.Health.Status == "healthy"
		icon := "●"
		if !healthy {
			icon = lipgloss.NewStyle().Foreground(styles.Danger).Render("●")
		} else {
			icon = lipgloss.NewStyle().Foreground(styles.Secondary).Render("●")
		}
		sessions := 0
		if m.snapshot.CodingSessions != nil {
			for _, s := range m.snapshot.CodingSessions {
				if s.Status == "running" {
					sessions++
				}
			}
		}
		total := len(m.snapshot.CodingSessions)
		ver := ""
		if m.snapshot.Health != nil && m.snapshot.Health.Version != "" {
			ver = " v" + m.snapshot.Health.Version
		}
		right = fmt.Sprintf("%s%s  [%d/%d sessions]", icon, ver, sessions, total)
	}

	screenLabel := ""
	if m.screen != screenDashboard {
		labels := map[screen]string{
			screenChat: "chat", screenSession: "session", screenMemory: "memory",
			screenUsage: "usage", screenTools: "tools", screenObservations: "awareness",
			screenDB: "database",
		}
		screenLabel = " › " + labels[m.screen]
	}

	gap := m.width - lipgloss.Width(left) - lipgloss.Width(right) - lipgloss.Width(screenLabel) - 4
	if gap < 0 {
		gap = 0
	}

	bar := left + screenLabel + strings.Repeat(" ", gap) + right
	return styles.HeaderStyle.Width(m.width).Render(bar)
}

func (m Model) renderFooter() string {
	var hints string
	if m.screen == screenDashboard {
		hints = hk("↑↓", "nav") + " " + hk("⏎", "open") + " " +
			hk("c", "chat") + " " + hk("m", "mem") + " " +
			hk("u", "usage") + " " + hk("t", "tools") + " " +
			hk("o", "obs") + " " + hk("b", "db") + " " +
			hk("n", "new") + " " + hk("p", "private") + " " +
			hk("r", "refresh") + " " + hk("tab", "focus") + " " +
			hk("q", "quit")
	} else if m.screen == screenChat {
		hints = hk("⏎", "send") + " " + hk("esc", "back") + " " + hk("ctrl+c", "quit")
	} else if m.screen == screenSession {
		hints = hk("⏎", "input") + " " + hk("s", "stop") + " " +
			hk("r", "refresh") + " " + hk("esc", "back")
	} else if m.screen == screenMemory {
		hints = hk("⏎", "search") + " " + hk("esc", "back")
	} else if m.screen == screenDB {
		hints = hk("↑↓", "nav") + " " + hk("⏎", "select") + " " +
			hk("⌫", "back") + " " + hk("/", "filter") + " " +
			hk("n/p", "page") + " " + hk("esc", "back")
	} else {
		hints = hk("r", "refresh") + " " + hk("esc", "back")
	}
	return styles.StatusBar.Width(m.width).Render(hints)
}

func hk(key, desc string) string {
	return styles.HelpKey.Render(key) + " " + styles.HelpDesc.Render(desc)
}

func (m Model) renderDashboard() string {
	// ---- Top-Left: Task Tree ----
	tlBorder := styles.PanelTop
	if m.quad == quadTopLeft {
		tlBorder = styles.PanelTopActive
	}
	tlTitle := styles.PanelTitle.Render(" Tasks")
	tlContent := m.sidebar.RenderContent()
	topLeft := tlBorder.Width(m.leftW - 2).Height(m.topH - 2).Render(
		lipgloss.JoinVertical(lipgloss.Left, tlTitle, tlContent))

	// ---- Top-Right: Session Detail ----
	trBorder := styles.PanelTop
	if m.quad == quadTopRight {
		trBorder = styles.PanelTopActive
	}
	trTitle := styles.PanelTitle.Render(" Session Detail")
	detailContent := m.detailText
	if detailContent == "" {
		detailContent = lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  Select a task or session")
	}
	topRight := trBorder.Width(m.rightW - 2).Height(m.topH - 2).Render(
		lipgloss.JoinVertical(lipgloss.Left, trTitle, "", detailContent))

	// ---- Bottom-Left: Tools Menu ----
	blBorder := styles.PanelBottom
	if m.quad == quadBotLeft {
		blBorder = styles.PanelBottomActive
	}
	blTitle := styles.PanelTitle.Render(" Tools")
	toolsContent := m.menu.RenderItems(m.botH - 4)
	botLeft := blBorder.Width(m.leftW - 2).Height(m.botH - 2).Render(
		lipgloss.JoinVertical(lipgloss.Left, blTitle, toolsContent))

	// ---- Bottom-Right: System Vitals ----
	brBorder := styles.PanelBottom
	if m.quad == quadBotRight {
		brBorder = styles.PanelBottomActive
	}
	brTitle := styles.PanelTitle.Render(" System Vitals")
	vitalsContent := m.vitals.RenderContent(m.rightW-6, m.botH-4)
	botRight := brBorder.Width(m.rightW - 2).Height(m.botH - 2).Render(
		lipgloss.JoinVertical(lipgloss.Left, brTitle, vitalsContent))

	// Compose grid
	topRow := lipgloss.JoinHorizontal(lipgloss.Top, topLeft, topRight)
	botRow := lipgloss.JoinHorizontal(lipgloss.Top, botLeft, botRight)

	return lipgloss.JoinVertical(lipgloss.Left, topRow, botRow)
}

func (m Model) renderSubScreen() string {
	bodyH := m.height - m.headerH - 1
	switch m.screen {
	case screenChat:
		m.chat.SetSize(m.width, bodyH)
		return m.chat.View()
	case screenSession:
		m.session.SetSize(m.width, bodyH)
		return m.session.View()
	case screenMemory:
		m.memBrowser.SetSize(m.width, bodyH)
		return m.memBrowser.View()
	case screenUsage:
		m.usageMonitor.SetSize(m.width, bodyH)
		return m.usageMonitor.View()
	case screenTools:
		m.toolsBrowser.SetSize(m.width, bodyH)
		return m.toolsBrowser.View()
	case screenObservations:
		m.obsView.SetSize(m.width, bodyH)
		return m.obsView.View()
	case screenDB:
		m.dbBrowser.SetSize(m.width, bodyH)
		return m.dbBrowser.View()
	}
	return ""
}

// ---- Async Commands ----

func tickCmd() tea.Cmd {
	return tea.Tick(3*time.Second, func(t time.Time) tea.Msg {
		return dashboardTick{}
	})
}

func fetchSnapshot(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		return snapshotLoaded{snap: client.FetchDashboardSnapshot()}
	}
}

func openConversation(client *api.Client, id string) tea.Cmd {
	return func() tea.Msg {
		conv, err := client.GetConversation(id, 100)
		if err != nil {
			return errMsg{err}
		}
		return conversationOpened{conv: conv}
	}
}

func createConversation(client *api.Client, agentSlug, title string) tea.Cmd {
	return createConversationOpts(client, agentSlug, title, false)
}

func createPrivateConversation(client *api.Client) tea.Cmd {
	return createConversationOpts(client, "", "Private Conversation", true)
}

func createConversationOpts(client *api.Client, agentSlug, title string, private bool) tea.Cmd {
	return func() tea.Msg {
		conv, err := client.CreateConversation(agentSlug, title, private)
		if err != nil {
			return errMsg{err}
		}
		return conversationCreated{conv: conv}
	}
}

func deleteConversation(client *api.Client, id string) tea.Cmd {
	return func() tea.Msg {
		if err := client.DeleteConversation(id); err != nil {
			return errMsg{err}
		}
		return conversationDeleted{id: id}
	}
}

func sendMessage(client *api.Client, convID, content string) tea.Cmd {
	return func() tea.Msg {
		ch, err := client.SendMessageStream(convID, content)
		if err != nil {
			return errMsg{err}
		}
		return streamStartMsg{ch: ch}
	}
}

func waitForChunk(ch <-chan api.StreamChunk) tea.Cmd {
	return func() tea.Msg {
		chunk, ok := <-ch
		if !ok {
			return streamDoneMsg{}
		}
		return streamChunkMsg{chunk: chunk}
	}
}

func loadCodingOutput(client *api.Client, sessionID string) tea.Cmd {
	return func() tea.Msg {
		output, err := client.GetCodingOutput(sessionID, 200)
		if err != nil {
			return errMsg{err}
		}
		return codingOutputLoaded{sessionID: sessionID, output: output}
	}
}

func sendCodingInput(client *api.Client, sessionID, text string) tea.Cmd {
	return func() tea.Msg {
		_ = client.SendCodingInput(sessionID, text)
		output, err := client.GetCodingOutput(sessionID, 200)
		if err != nil {
			return errMsg{err}
		}
		return codingOutputLoaded{sessionID: sessionID, output: output}
	}
}

func stopCodingSession(client *api.Client, sessionID string) tea.Cmd {
	return func() tea.Msg {
		_ = client.StopCodingSession(sessionID)
		return dashboardTick{}
	}
}

func loadMemories(client *api.Client, query string, limit int) tea.Cmd {
	return func() tea.Msg {
		var memories []api.Memory
		var err error
		if query == "" {
			memories, err = client.ListMemories(limit, 0)
		} else {
			memories, err = client.SearchMemories(query, limit)
		}
		if err != nil {
			return errMsg{err}
		}
		return memoriesLoaded{memories: memories}
	}
}

func loadUsageData(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		summary, _ := client.GetUsage(24)
		byAgent, _ := client.GetUsageByAgent(24)
		byModel, _ := client.GetUsageByModel(24)
		llm, _ := client.HealthLLM()
		return usageDataLoaded{summary: summary, byAgent: byAgent, byModel: byModel, llm: llm}
	}
}

func loadTools(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		tools, _ := client.ListTools()
		servers, _ := client.ListMCPServers()
		return toolsLoaded{tools: tools, servers: servers}
	}
}

func loadObservations(client *api.Client, limit int) tea.Cmd {
	return func() tea.Msg {
		obs, err := client.ListObservations(limit)
		if err != nil {
			return errMsg{err}
		}
		return observationsLoaded{obs: obs}
	}
}

func loadCollections(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		cols, err := client.ListCollections()
		if err != nil {
			return errMsg{err}
		}
		return collectionsLoaded{cols: cols}
	}
}

func queryCollection(client *api.Client, collection string, limit, skip int, filter string) tea.Cmd {
	return func() tea.Msg {
		result, err := client.QueryCollection(collection, limit, skip, filter)
		if err != nil {
			return errMsg{err}
		}
		return queryResultLoaded{result: result}
	}
}

func loadDocument(client *api.Client, collection, docID string) tea.Cmd {
	return func() tea.Msg {
		doc, err := client.GetDocument(collection, docID)
		if err != nil {
			return errMsg{err}
		}
		return documentLoaded{doc: doc}
	}
}
