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
	screenFleet
	screenHealth
	screenSearch
	screenNewSession
	screenHistory
	screenHistoryDetail
	screenProjects
	screenProjectCockpit
	screenModels
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
type codingSessionDeleted struct{ id string }
type streamStartMsg struct{ ch <-chan api.StreamChunk }
type streamChunkMsg struct{ chunk api.StreamChunk }
type streamDoneMsg struct{}
type codingOutputLoaded struct {
	sessionID string
	output    string
}
type codingLoopToggled struct {
	sessionID string
	enabled   bool
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
type fleetLoaded struct {
	sessions []api.CodingSession
	shells   []api.Shell
	usage    []api.SessionUsage
}
type healthLoaded struct{ health *api.ServicesHealth }

// modelServersLoaded carries the local LLM registry: which model+runtime pairs
// exist, how each is currently configured to load, and what memory each pool
// is holding.
type modelServersLoaded struct{ servers []api.ModelServer }

// modelServerActed reports the outcome of a start/stop. The message text is the
// point: a refusal names the conflicting server or the memory projection, and
// discarding it would leave the operator with a silent no-op.
type modelServerActed struct {
	status string
	reload bool
}
type searchResultLoaded struct {
	result *api.ToolExecuteResult
	err    error
}
type codingSessionCreated struct{ session *api.CodingSession }
type codingSessionCreateFailed struct{ err error }
type historyLoaded struct{ shells []api.ShellRecord }
type historyEventsLoaded struct {
	shell  *api.ShellRecord
	events []api.ShellEventRecord
}
type projectsLoaded struct{ overview api.ProjectsOverview }
type projectCockpitLoaded struct{ cockpit api.ProjectCockpit }
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
	memBrowser    *components.MemoryBrowser
	usageMonitor  *components.UsageMonitor
	toolsBrowser  *components.ToolsBrowser
	obsView       *components.ObservationsView
	dbBrowser     *components.DBBrowser
	fleetView     *components.FleetView
	healthView    *components.HealthView
	modelsView    *components.ModelsView
	searchView    *components.SearchView
	newSession    *components.NewSessionModal
	historyView   *components.HistoryView
	historyDetail *components.HistoryDetail
	projectsView  *components.ProjectsView
	cockpitView   *components.ProjectCockpitView

	// cockpitSlug is the project the cockpit screen currently shows -- kept on
	// the model so 'r' (refresh) knows what to re-fetch.
	cockpitSlug string

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

	// lastErr surfaces the most recent background-command failure in the
	// footer on WHATEVER screen is active. errMsg used to be handled only on
	// screenChat, so a failed API call anywhere else (e.g. a memory search
	// that 400s) failed completely silently -- the screen just stayed empty
	// with no indication anything went wrong. Cleared on navigation so it
	// doesn't linger once the user has moved on.
	lastErr string
}

func NewModel(client *api.Client) Model {
	return Model{
		client:        client,
		sidebar:       components.NewSidebar(),
		chat:          components.NewChatView(),
		session:       components.NewSessionView(),
		vitals:        components.NewVitalsPanel(),
		menu:          components.NewToolsMenu(),
		memBrowser:    components.NewMemoryBrowser(),
		usageMonitor:  components.NewUsageMonitor(),
		toolsBrowser:  components.NewToolsBrowser(),
		obsView:       components.NewObservationsView(),
		dbBrowser:     components.NewDBBrowser(),
		fleetView:     components.NewFleetView(),
		healthView:    components.NewHealthView(),
		modelsView:    components.NewModelsView(),
		searchView:    components.NewSearchView(),
		newSession:    components.NewNewSessionModal(),
		historyView:   components.NewHistoryView(),
		historyDetail: components.NewHistoryDetail(),
		projectsView:  components.NewProjectsView(),
		cockpitView:   components.NewProjectCockpitView(),
		screen:        screenDashboard,
		quad:          quadTopLeft,
		headerH:       1,
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
		cmd, consumed := m.handleKey(msg)
		if cmd != nil {
			cmds = append(cmds, cmd)
		}
		// If the key was an app-level action (submit, navigation, hotkey, esc),
		// don't also forward it to the focused child component. Forwarding a
		// handled Enter to the chat/session textarea, for instance, injects a
		// stray newline after every send. Unhandled keys (ordinary typing,
		// viewport scroll) fall through to the child updates below.
		if consumed {
			return m, tea.Batch(cmds...)
		}

	case dashboardTick:
		cmds = append(cmds, fetchSnapshot(m.client), tickCmd())

	case snapshotLoaded:
		m.snapshot = msg.snap
		if msg.snap != nil {
			m.agents = msg.snap.Agents
			m.sidebar.SetData(msg.snap.Agents, msg.snap.Conversations, msg.snap.CodingSessions, msg.snap.Shells)
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

	case codingSessionDeleted:
		if m.activeSessionID == msg.id {
			m.activeSessionID = ""
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

	case codingLoopToggled:
		if m.session.Session != nil && m.activeSessionID == msg.sessionID {
			m.session.Session.LoopEnabled = msg.enabled
		}
		// Reflect the toggle in the fleet table immediately; snapshot refresh
		// keeps the underlying data consistent.
		m.fleetView.SetLoopEnabled(msg.sessionID, msg.enabled)
		cmds = append(cmds, fetchSnapshot(m.client))

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

	case fleetLoaded:
		m.fleetView.SetData(msg.sessions, msg.shells, msg.usage)

	case healthLoaded:
		m.healthView.SetData(msg.health)

	case modelServersLoaded:
		m.modelsView.SetData(msg.servers)

	case modelServerActed:
		m.modelsView.Status = msg.status
		if msg.reload {
			return m, loadModelServers(m.client)
		}

	case historyLoaded:
		m.historyView.SetShells(msg.shells)

	case historyEventsLoaded:
		m.historyDetail.SetEvents(msg.shell, msg.events)

	case projectsLoaded:
		m.projectsView.SetData(msg.overview)

	case projectCockpitLoaded:
		m.cockpitView.SetData(msg.cockpit)

	case searchResultLoaded:
		errStr := ""
		if msg.err != nil {
			errStr = msg.err.Error()
		}
		m.searchView.SetResult(msg.result, errStr)

	case codingSessionCreated:
		// Land on the new session's own view, not back on the modal --
		// popScreen first so the session's prevScreen is the dashboard
		// (where the modal came from), not the now-stale modal itself.
		m.popScreen()
		cmds = append(cmds, m.openSession(msg.session.ID, msg.session), fetchSnapshot(m.client))

	case codingSessionCreateFailed:
		m.newSession.SetErr(msg.err.Error())

	case errMsg:
		if m.screen == screenChat {
			m.chat.AppendStreamChunk("\n[Error: " + msg.err.Error() + "]")
			m.chat.FinishStream()
		} else {
			m.lastErr = msg.err.Error()
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
	} else if m.screen == screenFleet {
		var cmd tea.Cmd
		m.fleetView, cmd = m.fleetView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenHealth {
		var cmd tea.Cmd
		m.healthView, cmd = m.healthView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenSearch {
		var cmd tea.Cmd
		m.searchView, cmd = m.searchView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenNewSession {
		var cmd tea.Cmd
		m.newSession, cmd = m.newSession.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenHistory {
		var cmd tea.Cmd
		m.historyView, cmd = m.historyView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenHistoryDetail {
		var cmd tea.Cmd
		m.historyDetail, cmd = m.historyDetail.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenProjects {
		var cmd tea.Cmd
		m.projectsView, cmd = m.projectsView.Update(msg)
		cmds = append(cmds, cmd)
	} else if m.screen == screenProjectCockpit {
		var cmd tea.Cmd
		m.cockpitView, cmd = m.cockpitView.Update(msg)
		cmds = append(cmds, cmd)
	}

	return m, tea.Batch(cmds...)
}

// ---- Key Handling ----

// handleKey routes a key press and reports whether it was consumed as an
// app-level action. When consumed is false the key is ordinary input (typing,
// scrolling) and the caller forwards it to the focused child component.
func (m *Model) handleKey(msg tea.KeyMsg) (cmd tea.Cmd, consumed bool) {
	key := msg.String()

	if key == "ctrl+c" {
		return tea.Quit, true
	}

	if m.screen != screenDashboard {
		return m.handleSubScreenKey(msg)
	}
	// The dashboard has no text-input child to forward to, so every key it sees
	// is an app-level action.
	return m.handleDashboardKey(key), true
}

func (m *Model) handleDashboardKey(key string) tea.Cmd {
	switch key {
	case "q":
		return tea.Quit

	// Focus toggles between the two interactive panes: the Tasks sidebar
	// (top-left) and the Tools menu (bottom-left). The right-hand panes are
	// read-only info, so they are not focus stops.
	case "tab", "shift+tab":
		if m.quad == quadBotLeft {
			m.quad = quadTopLeft
		} else {
			m.quad = quadBotLeft
		}

	// Navigation is routed to whichever pane has focus.
	case "up", "k":
		if m.quad == quadBotLeft {
			m.menu.Up()
		} else {
			m.sidebar.Up()
			m.updateDetail()
		}
	case "down", "j":
		if m.quad == quadBotLeft {
			m.menu.Down()
		} else {
			m.sidebar.Down()
			m.updateDetail()
		}
	case "enter":
		if m.quad == quadBotLeft {
			// Launch the highlighted Tools-menu entry.
			return m.openHotkey(m.menu.Selected().Key)
		}
		node := m.sidebar.Selected()
		if node == nil {
			return nil
		}
		switch node.Kind {
		case components.NodeCodingAgentGroup:
			m.newSession.Reset(node.Label, node.SessionBackend, node.SessionProfile)
			m.pushScreen(screenNewSession)
			return nil
		case components.NodeAgent:
			// Other Agents entries only, as of the 2026-07-31 sidebar
			// restructure (Coding Agents moved to NodeCodingAgentGroup
			// above). The Hermes stub has no live backend to act on; a
			// disabled agent (Search Agent while paused) would just 400 --
			// both explain themselves in the detail panel instead of
			// silently doing nothing or firing a call that can't succeed.
			if node.IsStub || node.Agent == nil || !node.Agent.Enabled {
				return nil
			}
			return createConversation(m.client, node.ID, "")
		case components.NodeConversation:
			return openConversation(m.client, node.ID)
		case components.NodeCodingSession:
			return m.openSession(node.ID, node.CodingSession)
		}

	// New/private conversation + delete + refresh work from either pane.
	case "n":
		return createConversation(m.client, "", "")
	case "p":
		return createPrivateConversation(m.client)
	case "d":
		node := m.sidebar.Selected()
		if node == nil {
			return nil
		}
		switch node.Kind {
		case components.NodeConversation:
			return deleteConversation(m.client, node.ID)
		case components.NodeCodingSession:
			// The server already refuses to delete a running/queued session
			// (stop it first) -- checking here too so 'd' on a live session is
			// a silent no-op instead of a round trip that comes back 409.
			if node.CodingSession != nil && (node.CodingSession.Status == "running" || node.CodingSession.Status == "queued") {
				return nil
			}
			return deleteCodingSession(m.client, node.ID)
		}
	case "r":
		return fetchSnapshot(m.client)

	// Single-letter screen launchers (also reachable via the Tools menu).
	default:
		return m.openHotkey(key)
	}
	return nil
}

// openHotkey launches a screen by its single-letter key. Shared by the bare
// dashboard hotkeys and Enter on the Tools menu so the two never drift apart.
func (m *Model) openHotkey(key string) tea.Cmd {
	switch key {
	case "c":
		slug := ""
		for _, a := range m.agents {
			if a.IsDefault {
				slug = a.Slug
				break
			}
		}
		return createConversation(m.client, slug, "")
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
	case "f":
		m.pushScreen(screenFleet)
		return loadFleet(m.client, m.snapshot)
	case "h":
		m.pushScreen(screenHealth)
		return loadServicesHealth(m.client)
	case "s":
		m.pushScreen(screenSearch)
		return nil
	case "y":
		m.pushScreen(screenHistory)
		return loadHistory(m.client)
	case "j":
		m.pushScreen(screenProjects)
		return loadProjects(m.client)
	case "g":
		m.pushScreen(screenModels)
		return loadModelServers(m.client)
	}
	return nil
}

// openSession attaches to a coding session and shows its full-screen view.
func (m *Model) openSession(id string, cs *api.CodingSession) tea.Cmd {
	m.activeSessionID = id
	m.session.SetSession(cs)
	m.pushScreen(screenSession)
	return loadCodingOutput(m.client, id)
}

// openHistoryDetail shows a historical shell's stored scrollback.
func (m *Model) openHistoryDetail(shell api.ShellRecord) tea.Cmd {
	m.historyView.Blur()
	m.pushScreen(screenHistoryDetail)
	return loadHistoryEvents(m.client, shell)
}

// openProjectCockpit shows one project's cockpit. Same two-level shape as
// history -> history detail: blur the parent list, push, load -- esc from the
// cockpit then pops back to the projects list.
func (m *Model) openProjectCockpit(slug string) tea.Cmd {
	m.cockpitSlug = slug
	m.projectsView.Blur()
	m.pushScreen(screenProjectCockpit)
	return loadProjectCockpit(m.client, slug)
}

func (m *Model) handleSubScreenKey(msg tea.KeyMsg) (tea.Cmd, bool) {
	key := msg.String()

	// The DB filter editor is a focused text field: it swallows every key
	// (including esc/enter) until dismissed, so handle it before the generic
	// esc-to-back below.
	if m.screen == screenDB && m.dbBrowser.IsEditing() {
		switch key {
		case "esc":
			m.dbBrowser.ToggleEditing()
		case "enter":
			m.dbBrowser.ToggleEditing()
			col := m.dbBrowser.CurrentCollection()
			if col != "" {
				m.dbBrowser.SetPage(0)
				return queryCollection(m.client, col, 20, 0, m.dbBrowser.GetFilter()), true
			}
		default:
			m.dbBrowser.HandleFilterKey(key)
		}
		return nil, true
	}

	// Same swallow-everything-until-dismissed shape as the DB filter editor
	// above, for History's filter box.
	if m.screen == screenHistory && m.historyView.Editing {
		switch key {
		case "esc", "enter":
			m.historyView.ExitFilterEdit()
		default:
			m.historyView.UpdateFilterInput(msg)
		}
		return nil, true
	}

	if key == "esc" {
		m.popScreen()
		return nil, true
	}

	switch m.screen {
	case screenChat:
		if key == "enter" {
			if m.chat.Streaming {
				return nil, true
			}
			input := m.chat.GetInput()
			if input == "" {
				return nil, true
			}
			m.chat.Messages = append(m.chat.Messages, api.Message{Role: "user", Content: input})
			m.chat.StreamBuffer = ""
			m.chat.Streaming = true
			m.chat.SetMessages(m.chat.Messages)
			return sendMessage(m.client, m.activeConvID, input), true
		}
	case screenSession:
		// The session's input box is a focused textarea, so session actions use
		// ctrl-modified keys to avoid clobbering ordinary typing.
		switch key {
		case "enter":
			input := m.session.GetInput()
			if input != "" {
				return sendCodingInput(m.client, m.activeSessionID, input), true
			}
			return nil, true
		case "ctrl+s":
			if m.activeSessionID != "" {
				return stopCodingSession(m.client, m.activeSessionID), true
			}
			return nil, true
		case "ctrl+l":
			if m.activeSessionID != "" {
				enable := true
				if m.session.Session != nil {
					enable = !m.session.Session.LoopEnabled
				}
				return toggleCodingLoop(m.client, m.activeSessionID, enable), true
			}
			return nil, true
		case "ctrl+r":
			if m.activeSessionID != "" {
				return loadCodingOutput(m.client, m.activeSessionID), true
			}
			return nil, true
		}
	case screenMemory:
		if key == "enter" {
			query := m.memBrowser.GetQuery()
			if query == "" {
				return loadMemories(m.client, "", 50), true
			}
			return loadMemories(m.client, query, 20), true
		}
	case screenFleet:
		switch key {
		case "enter":
			if s := m.fleetView.SelectedSession(); s != nil {
				return m.openSession(s.ID, s), true
			}
			return nil, true
		case "r":
			return loadFleet(m.client, m.snapshot), true
		case "up", "k":
			m.fleetView.MoveCursor(-1)
			return nil, true
		case "down", "j":
			m.fleetView.MoveCursor(1)
			return nil, true
		case "l":
			if s := m.fleetView.SelectedSession(); s != nil {
				return toggleCodingLoop(m.client, s.ID, !s.LoopEnabled), true
			}
			return nil, true
		}
	case screenHistory:
		switch key {
		case "enter":
			if s := m.historyView.Selected(); s != nil {
				return m.openHistoryDetail(*s), true
			}
			return nil, true
		case "r":
			return loadHistory(m.client), true
		case "/":
			m.historyView.EnterFilterEdit()
			return nil, true
		case "up", "k":
			m.historyView.MoveCursor(-1)
			return nil, true
		case "down", "j":
			m.historyView.MoveCursor(1)
			return nil, true
		}
	case screenProjects:
		switch key {
		case "enter":
			if p := m.projectsView.SelectedProject(); p != nil {
				return m.openProjectCockpit(p.Slug), true
			}
			return nil, true
		case "f":
			if p := m.projectsView.SelectedProject(); p != nil {
				return setActiveProject(m.client, p.Slug), true
			}
			return nil, true
		case "r":
			return loadProjects(m.client), true
		case "up", "k":
			m.projectsView.MoveCursor(-1)
			return nil, true
		case "down", "j":
			m.projectsView.MoveCursor(1)
			return nil, true
		}
	case screenProjectCockpit:
		if key == "r" {
			if m.cockpitSlug != "" {
				return loadProjectCockpit(m.client, m.cockpitSlug), true
			}
			return nil, true
		}
	case screenNewSession:
		submit, consumed := m.newSession.HandleKey(key)
		if !consumed {
			return nil, false // ordinary typing (or a literal space) -- fall through to Update()
		}
		if submit {
			repo, prompt, useWorktree, worktreeName := m.newSession.Values()
			m.newSession.Submitting = true
			return createCodingSession(m.client, api.CreateCodingSessionRequest{
				Workspace:       repo,
				Prompt:          prompt,
				Backend:         m.newSession.SessionBackend,
				SubagentProfile: m.newSession.SessionProfile,
				CreateWorktree:  useWorktree,
				WorktreeName:    worktreeName,
			}), true
		}
		return nil, true
	case screenModels:
		// Inline editing owns the keyboard first — otherwise typing "8" into a
		// context size would be read as a screen action.
		if m.modelsView.HandleEditKey(key) {
			return nil, true
		}
		s := m.modelsView.Selected()
		switch key {
		case "up", "k":
			m.modelsView.MoveCursor(-1)
			return nil, true
		case "down", "j":
			m.modelsView.MoveCursor(1)
			return nil, true
		case "left", "h":
			m.modelsView.CycleChoice(-1)
			return nil, true
		case "right", "l":
			m.modelsView.CycleChoice(1)
			return nil, true
		case "tab":
			m.modelsView.ToggleParamFocus()
			return nil, true
		case "enter":
			m.modelsView.BeginEdit()
			return nil, true
		case "r":
			return loadModelServers(m.client), true
		case "s":
			if s == nil {
				return nil, true
			}
			if err := m.modelsView.ValidateDraft(); err != nil {
				m.modelsView.Status = err.Error()
				return nil, true
			}
			m.modelsView.Status = "starting " + s.Slug + " — " + m.modelsView.ParamSummary()
			return startModelServer(m.client, s.Slug, m.modelsView.Overrides()), true
		case "d":
			// Deliberately distinct from "s": a plain start also CLEARS any
			// override ARIA applied earlier, so a context size chosen for one
			// experiment cannot silently outlive it.
			if s == nil {
				return nil, true
			}
			m.modelsView.ClearDraft()
			m.modelsView.Status = "starting " + s.Slug + " with deployment defaults"
			return startModelServer(m.client, s.Slug, nil), true
		case "x":
			if s == nil {
				return nil, true
			}
			m.modelsView.Status = "stopping " + s.Slug
			return stopModelServer(m.client, s.Slug), true
		}
	case screenUsage, screenTools, screenObservations, screenHealth:
		if key == "r" {
			switch m.screen {
			case screenUsage:
				return loadUsageData(m.client), true
			case screenTools:
				return loadTools(m.client), true
			case screenObservations:
				return loadObservations(m.client, 50), true
			case screenHealth:
				return loadServicesHealth(m.client), true
			}
		}
	case screenSearch:
		if key == "enter" {
			query := m.searchView.GetQuery()
			if query == "" {
				return nil, true
			}
			m.searchView.SetSearching(true)
			return runSearch(m.client, query), true
		}
	case screenDB:
		switch key {
		case "up", "k":
			m.dbBrowser.Up()
			return nil, true
		case "down", "j":
			m.dbBrowser.Down()
			return nil, true
		case "enter":
			switch m.dbBrowser.Mode() {
			case 0: // collections
				col := m.dbBrowser.SelectedCollection()
				if col != "" {
					m.dbBrowser.SetCollection(col)
					m.dbBrowser.SetPage(0)
					m.dbBrowser.SetFilter("")
					return queryCollection(m.client, col, 20, 0, ""), true
				}
			case 1: // documents
				docID := m.dbBrowser.SelectedDocID()
				col := m.dbBrowser.CurrentCollection()
				if docID != "" && col != "" {
					return loadDocument(m.client, col, docID), true
				}
			}
			return nil, true
		case "backspace":
			if !m.dbBrowser.GoBack() {
				m.popScreen()
			}
			return nil, true
		case "/":
			if m.dbBrowser.Mode() == 1 { // documents mode
				m.dbBrowser.ToggleEditing()
			}
			return nil, true
		case "n":
			if m.dbBrowser.Mode() == 1 { // next page
				col := m.dbBrowser.CurrentCollection()
				p := m.dbBrowser.Page() + 1
				m.dbBrowser.SetPage(p)
				return queryCollection(m.client, col, 20, p*20, m.dbBrowser.GetFilter()), true
			}
			return nil, true
		case "p":
			if m.dbBrowser.Mode() == 1 && m.dbBrowser.Page() > 0 { // prev page
				col := m.dbBrowser.CurrentCollection()
				p := m.dbBrowser.Page() - 1
				m.dbBrowser.SetPage(p)
				return queryCollection(m.client, col, 20, p*20, m.dbBrowser.GetFilter()), true
			}
			return nil, true
		case "ctrl+u":
			for i := 0; i < 10; i++ {
				m.dbBrowser.ScrollUp()
			}
			return nil, true
		case "ctrl+d":
			for i := 0; i < 10; i++ {
				m.dbBrowser.ScrollDown()
			}
			return nil, true
		}
	}
	return nil, false
}

// ---- Screen Stack ----

func (m *Model) pushScreen(s screen) {
	m.prevScreen = m.screen
	m.screen = s
	m.lastErr = ""
	if s == screenChat {
		m.chat.Focus()
	} else if s == screenSession {
		m.session.Focus()
	} else if s == screenMemory {
		m.memBrowser.Focus()
	} else if s == screenDB {
		m.dbBrowser.Focus()
	} else if s == screenSearch {
		m.searchView.Focus()
	} else if s == screenHistory {
		m.historyView.Focus()
	} else if s == screenHistoryDetail {
		m.historyDetail.Focus()
	} else if s == screenProjects {
		m.projectsView.Focus()
	} else if s == screenProjectCockpit {
		m.cockpitView.Focus()
	}
}

func (m *Model) popScreen() {
	m.chat.Blur()
	m.session.Blur()
	m.memBrowser.Blur()
	m.dbBrowser.Blur()
	m.searchView.Blur()
	m.historyView.Blur()
	m.historyDetail.Blur()
	m.projectsView.Blur()
	m.cockpitView.Blur()
	m.screen = m.prevScreen
	m.prevScreen = screenDashboard
	m.lastErr = ""
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
	case components.NodeCodingAgentGroup:
		b.WriteString(styles.PanelTitle.Render(node.Label) + "\n")
		if node.SessionProfile != "" && node.Agent != nil {
			b.WriteString(styles.VitalLabel.Render("  Model: ") + node.Agent.LLM.Backend + "/" + node.Agent.LLM.Model + "\n")
		} else if node.SessionBackend != "" {
			b.WriteString(styles.VitalLabel.Render("  Backend: ") + node.SessionBackend + "\n")
		}
		b.WriteString("\n" + styles.HelpKey.Render("  Enter") + styles.HelpDesc.Render(" start a new session here"))
	case components.NodeAgent:
		if node.IsStub {
			b.WriteString(styles.PanelTitle.Render(node.Label) + "\n")
			b.WriteString(lipgloss.NewStyle().Foreground(styles.SubText).Render(
				"  Hermes has no chat/query API for this TUI to call yet -- only\n"+
					"  a narrow inbound webhook listener. Reachable today via the\n"+
					"  hermes CLI or its own web dashboard, not from here.") + "\n")
			break
		}
		a := node.Agent
		if a != nil {
			b.WriteString(styles.PanelTitle.Render("Agent: "+a.Name) + "\n")
			b.WriteString(styles.VitalLabel.Render("  Slug: ") + a.Slug + "\n")
			b.WriteString(styles.VitalLabel.Render("  Backend: ") + a.LLM.Backend + "/" + a.LLM.Model + "\n")
			b.WriteString(styles.VitalLabel.Render("  Category: ") + a.ModeCategory + "\n")
			if !a.Enabled {
				b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.Warning).Render("  Disabled -- re-enable it before starting a conversation.") + "\n")
			}
			if a.Description != "" {
				b.WriteString("\n" + lipgloss.NewStyle().Foreground(styles.SubText).Render("  "+a.Description) + "\n")
			}
			if a.Enabled {
				b.WriteString("\n" + styles.HelpKey.Render("  Enter") + styles.HelpDesc.Render(" start conversation"))
			}
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
			if cs.SourceRepo != "" {
				b.WriteString(styles.VitalLabel.Render("  Source repo: ") + cs.SourceRepo + "\n")
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
	m.fleetView.SetSize(m.width, bodyH)
	m.healthView.SetSize(m.width, bodyH)
	m.searchView.SetSize(m.width, bodyH)
	m.newSession.SetSize(m.width, bodyH)
	m.historyView.SetSize(m.width, bodyH)
	m.historyDetail.SetSize(m.width, bodyH)
	m.projectsView.SetSize(m.width, bodyH)
	m.cockpitView.SetSize(m.width, bodyH)

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

	out := lipgloss.JoinVertical(lipgloss.Left, header, body, footer)

	// Safety net: never emit more rows than the terminal has. With the alt-screen
	// buffer, an over-tall frame scrolls the top (the header) off; clamping to
	// m.height keeps the header pinned instead of letting it disappear.
	if m.height > 0 {
		if lines := strings.Split(out, "\n"); len(lines) > m.height {
			out = strings.Join(lines[:m.height], "\n")
		}
	}
	return out
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
			screenDB: "database", screenFleet: "fleet", screenHealth: "health",
			screenSearch: "search", screenNewSession: "new session",
			screenHistory: "history", screenHistoryDetail: "history › scrollback",
			screenProjects: "projects", screenProjectCockpit: "projects › cockpit",
			screenModels: "models",
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
			hk("f", "fleet") + " " + hk("h", "health") + " " +
			hk("s", "search") + " " + hk("y", "history") + " " +
			hk("n", "new") + " " + hk("p", "private") + " " +
			hk("d", "delete") + " " +
			hk("r", "refresh") + " " + hk("tab", "tasks/tools") + " " +
			hk("q", "quit")
	} else if m.screen == screenSearch {
		hints = hk("⏎", "search") + " " + hk("esc", "back")
	} else if m.screen == screenNewSession {
		hints = hk("tab", "field") + " " + hk("space", "toggle") + " " +
			hk("^s", "start") + " " + hk("esc", "cancel")
	} else if m.screen == screenChat {
		hints = hk("⏎", "send") + " " + hk("esc", "back") + " " + hk("ctrl+c", "quit")
	} else if m.screen == screenSession {
		hints = hk("⏎", "input") + " " + hk("^s", "stop") + " " +
			hk("^l", "loop") + " " +
			hk("^r", "refresh") + " " + hk("esc", "back")
	} else if m.screen == screenMemory {
		hints = hk("⏎", "search") + " " + hk("esc", "back")
	} else if m.screen == screenProjects {
		hints = hk("⏎", "cockpit") + " " + hk("f", "focus") + " " +
			hk("r", "refresh") + " " + hk("esc", "back")
	} else if m.screen == screenDB {
		hints = hk("↑↓", "nav") + " " + hk("⏎", "select") + " " +
			hk("⌫", "back") + " " + hk("/", "filter") + " " +
			hk("n/p", "page") + " " + hk("esc", "back")
	} else {
		hints = hk("r", "refresh") + " " + hk("esc", "back")
	}
	if m.lastErr != "" {
		hints = lipgloss.NewStyle().Foreground(styles.Danger).Render("⚠ "+m.lastErr) + "   " + hints
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
	topLeft := tlBorder.Width(m.leftW - 2).Height(m.topH - 2).MaxHeight(m.topH).Render(
		lipgloss.JoinVertical(lipgloss.Left, tlTitle, tlContent))

	// ---- Top-Right: Session Detail (read-only mirror of the selection) ----
	trBorder := styles.PanelTop
	trTitle := styles.PanelTitle.Render(" Session Detail")
	detailContent := m.detailText
	if detailContent == "" {
		detailContent = lipgloss.NewStyle().Foreground(styles.Muted).Render("\n  Select a task or session")
	}
	topRight := trBorder.Width(m.rightW - 2).Height(m.topH - 2).MaxHeight(m.topH).Render(
		lipgloss.JoinVertical(lipgloss.Left, trTitle, "", detailContent))

	// ---- Bottom-Left: Tools Menu ----
	blBorder := styles.PanelBottom
	if m.quad == quadBotLeft {
		blBorder = styles.PanelBottomActive
	}
	blTitle := styles.PanelTitle.Render(" Tools")
	toolsContent := m.menu.RenderItems(m.botH-4, m.quad == quadBotLeft)
	botLeft := blBorder.Width(m.leftW - 2).Height(m.botH - 2).MaxHeight(m.botH).Render(
		lipgloss.JoinVertical(lipgloss.Left, blTitle, toolsContent))

	// ---- Bottom-Right: System Vitals (read-only) ----
	brBorder := styles.PanelBottom
	brTitle := styles.PanelTitle.Render(" System Vitals")
	vitalsContent := m.vitals.RenderContent(m.rightW-6, m.botH-4)
	botRight := brBorder.Width(m.rightW - 2).Height(m.botH - 2).MaxHeight(m.botH).Render(
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
	case screenFleet:
		m.fleetView.SetSize(m.width, bodyH)
		return m.fleetView.View()
	case screenHealth:
		m.healthView.SetSize(m.width, bodyH)
		return m.healthView.View()
	case screenModels:
		m.modelsView.SetSize(m.width, bodyH)
		return m.modelsView.View()
	case screenSearch:
		m.searchView.SetSize(m.width, bodyH)
		return m.searchView.View()
	case screenNewSession:
		m.newSession.SetSize(m.width, bodyH)
		return m.newSession.View()
	case screenHistory:
		m.historyView.SetSize(m.width, bodyH)
		return m.historyView.View()
	case screenHistoryDetail:
		m.historyDetail.SetSize(m.width, bodyH)
		return m.historyDetail.View()
	case screenProjects:
		m.projectsView.SetSize(m.width, bodyH)
		return m.projectsView.View()
	case screenProjectCockpit:
		m.cockpitView.SetSize(m.width, bodyH)
		return m.cockpitView.View()
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

// createCodingSession starts a real coding session -- the New Session
// modal's submit action. Mirrors the web UI's equivalent call; see
// api.Client.CreateCodingSession for the worktree-provisioning contract.
func createCodingSession(client *api.Client, req api.CreateCodingSessionRequest) tea.Cmd {
	return func() tea.Msg {
		session, err := client.CreateCodingSession(req)
		if err != nil {
			return codingSessionCreateFailed{err: err}
		}
		return codingSessionCreated{session: session}
	}
}

func stopCodingSession(client *api.Client, sessionID string) tea.Cmd {
	return func() tea.Msg {
		_ = client.StopCodingSession(sessionID)
		return dashboardTick{}
	}
}

func deleteCodingSession(client *api.Client, sessionID string) tea.Cmd {
	return func() tea.Msg {
		if err := client.DeleteCodingSession(sessionID); err != nil {
			return errMsg{err}
		}
		return codingSessionDeleted{id: sessionID}
	}
}

func toggleCodingLoop(client *api.Client, sessionID string, enabled bool) tea.Cmd {
	return func() tea.Msg {
		if err := client.ToggleCodingLoop(sessionID, enabled); err != nil {
			return errMsg{err}
		}
		return codingLoopToggled{sessionID: sessionID, enabled: enabled}
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

func loadFleet(client *api.Client, snap *api.DashboardSnapshot) tea.Cmd {
	return func() tea.Msg {
		usage, _ := client.GetUsageBySession()
		var sessions []api.CodingSession
		var shells []api.Shell
		if snap != nil {
			sessions = snap.CodingSessions
			shells = snap.Shells
		} else {
			sessions, _ = client.ListCodingSessions("")
			shells, _ = client.ListShells()
		}
		return fleetLoaded{sessions: sessions, shells: shells, usage: usage}
	}
}

func loadServicesHealth(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		health, err := client.GetServicesHealth()
		if err != nil {
			return errMsg{err}
		}
		return healthLoaded{health: health}
	}
}

func loadModelServers(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		servers, err := client.ListModelServers()
		if err != nil {
			return errMsg{err}
		}
		return modelServersLoaded{servers: servers}
	}
}

func startModelServer(client *api.Client, slug string, overrides map[string]string) tea.Cmd {
	return func() tea.Msg {
		if err := client.StartModelServer(slug, overrides); err != nil {
			// Surfaced rather than raised as a generic error: a refusal here
			// explains WHICH server conflicts or how far over the memory
			// margin the projection lands, and that text is the whole value.
			return modelServerActed{status: err.Error(), reload: true}
		}
		return modelServerActed{
			status: slug + " starting — model load takes ~2-3 min before it answers",
			reload: true,
		}
	}
}

func stopModelServer(client *api.Client, slug string) tea.Cmd {
	return func() tea.Msg {
		if err := client.StopModelServer(slug); err != nil {
			return modelServerActed{status: err.Error(), reload: true}
		}
		return modelServerActed{status: slug + " stopped", reload: true}
	}
}

func loadHistory(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		shells, err := client.ListAllShells()
		if err != nil {
			return errMsg{err}
		}
		return historyLoaded{shells: shells}
	}
}

// loadHistoryEvents fetches a shell's tail scrollback -- the events endpoint
// has no "last N" shorthand, so the tail is approximated from the shell's
// own line_count (captured when the list was loaded, not re-fetched here).
func loadHistoryEvents(client *api.Client, shell api.ShellRecord) tea.Cmd {
	return func() tea.Msg {
		const windowSize = 800
		sinceLine := shell.LineCount - windowSize
		if sinceLine < 0 {
			sinceLine = 0
		}
		events, err := client.GetShellEvents(shell.Name, sinceLine, windowSize)
		if err != nil {
			return errMsg{err}
		}
		return historyEventsLoaded{shell: &shell, events: events}
	}
}

func loadProjects(client *api.Client) tea.Cmd {
	return func() tea.Msg {
		overview, err := client.GetProjectsOverview()
		if err != nil {
			return errMsg{err}
		}
		return projectsLoaded{overview: *overview}
	}
}

func loadProjectCockpit(client *api.Client, slug string) tea.Cmd {
	return func() tea.Msg {
		cockpit, err := client.GetProjectCockpit(slug)
		if err != nil {
			return errMsg{err}
		}
		return projectCockpitLoaded{cockpit: *cockpit}
	}
}

// setActiveProject persists the server-side focus, then reloads the overview
// so the ★ marker reflects the new state.
func setActiveProject(client *api.Client, slug string) tea.Cmd {
	return func() tea.Msg {
		if err := client.SetActiveProject(slug); err != nil {
			return errMsg{err}
		}
		overview, err := client.GetProjectsOverview()
		if err != nil {
			return errMsg{err}
		}
		return projectsLoaded{overview: *overview}
	}
}

func runSearch(client *api.Client, query string) tea.Cmd {
	return func() tea.Msg {
		result, err := client.ExecuteTool("search_agent", map[string]interface{}{"query": query})
		return searchResultLoaded{result: result, err: err}
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
