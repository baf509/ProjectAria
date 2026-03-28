package styles

import "github.com/charmbracelet/lipgloss"

// ---- Color Palette (Catppuccin Mocha) ----

var (
	// Core semantic colors
	Primary    = lipgloss.Color("#7c3aed") // Purple — top panels, main accent
	Secondary  = lipgloss.Color("#22c55e") // Green  — bottom panels, success
	Accent     = lipgloss.Color("#38bdf8") // Sky blue — titles, highlights
	Muted      = lipgloss.Color("#6b7280") // Gray — secondary text
	Danger     = lipgloss.Color("#ef4444") // Red — errors, failures
	Warning    = lipgloss.Color("#f59e0b") // Amber — warnings, paused
	Info       = lipgloss.Color("#38bdf8") // Sky blue — info observations
	Surface    = lipgloss.Color("#1e1e2e") // Dark surface
	Surface1   = lipgloss.Color("#313244") // Lighter surface
	Text       = lipgloss.Color("#cdd6f4") // Primary text
	SubText    = lipgloss.Color("#a6adc8") // Muted text
	Overlay    = lipgloss.Color("#585b70") // Subtle borders/dividers

	// Border colors
	BorderColor  = lipgloss.Color("#45475a") // General border/divider color
	BorderDim    = lipgloss.Color("#45475a")
	BorderTop    = Primary   // Top panel borders
	BorderBottom = Secondary // Bottom panel borders

	// ---- Header Bar ----
	HeaderStyle = lipgloss.NewStyle().
			Background(Primary).
			Foreground(lipgloss.Color("#ffffff")).
			Bold(true).
			Padding(0, 1)

	// ---- Panel Titles ----
	PanelTitle = lipgloss.NewStyle().
			Foreground(Accent).
			Bold(true).
			Padding(0, 0, 0, 0)

	// ---- Panel Borders ----
	PanelTop = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(BorderTop)

	PanelTopActive = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(lipgloss.Color("#a78bfa")) // Lighter purple

	PanelBottom = lipgloss.NewStyle().
			Border(lipgloss.RoundedBorder()).
			BorderForeground(BorderBottom)

	PanelBottomActive = lipgloss.NewStyle().
				Border(lipgloss.RoundedBorder()).
				BorderForeground(lipgloss.Color("#4ade80")) // Lighter green

	// Legacy compat
	PaneBorder       = PanelTop
	PaneBorderActive = PanelTopActive

	// ---- Title Bar (within panels) ----
	TitleStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(Primary).
			Padding(0, 1)

	SectionTitle = lipgloss.NewStyle().
			Foreground(Accent).
			Bold(true)

	// ---- Footer / Status Bar ----
	StatusBar = lipgloss.NewStyle().
			Background(lipgloss.Color("#181825")).
			Foreground(SubText).
			Padding(0, 1)

	StatusKey = lipgloss.NewStyle().
			Foreground(Warning).
			Bold(true)

	// ---- Messages ----
	UserMessage = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#89b4fa")).
			Bold(true)

	AssistantMessage = lipgloss.NewStyle().
				Foreground(Text)

	SystemMessage = lipgloss.NewStyle().
			Foreground(Muted).
			Italic(true)

	ToolMessage = lipgloss.NewStyle().
			Foreground(Secondary)

	// ---- Tree / Sidebar ----
	SidebarItem = lipgloss.NewStyle().
			Padding(0, 1)

	SidebarSelected = lipgloss.NewStyle().
			Padding(0, 1).
			Background(lipgloss.Color("#313244")).
			Foreground(lipgloss.Color("#ffffff")).
			Bold(true)

	TreeIndent = lipgloss.NewStyle().
			Foreground(Muted)

	// ---- Input ----
	InputPrompt = lipgloss.NewStyle().
			Foreground(Primary).
			Bold(true)

	// ---- Help Keys ----
	HelpKey = lipgloss.NewStyle().
		Foreground(Warning).
		Bold(true)

	HelpDesc = lipgloss.NewStyle().
		Foreground(Muted)

	// ---- Spinner ----
	SpinnerStyle = lipgloss.NewStyle().
			Foreground(Primary)

	// ---- Vitals / Stats ----
	VitalLabel = lipgloss.NewStyle().
			Foreground(SubText)

	VitalValue = lipgloss.NewStyle().
			Foreground(Text).
			Bold(true)

	VitalGood = lipgloss.NewStyle().
			Foreground(Secondary).
			Bold(true)

	VitalWarn = lipgloss.NewStyle().
			Foreground(Warning).
			Bold(true)

	VitalBad = lipgloss.NewStyle().
			Foreground(Danger).
			Bold(true)

	// ---- Log Viewer ----
	LogTimestamp = lipgloss.NewStyle().
			Foreground(Muted)

	LogContent = lipgloss.NewStyle().
			Foreground(Text)

	// ---- Divider ----
	Divider = lipgloss.NewStyle().
		Foreground(Overlay)

	// ---- Agent tags (unused currently but available) ----
	AgentTag = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#1e1e2e")).
			Background(Secondary).
			Padding(0, 1).
			Bold(true)

	CodingTag = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#1e1e2e")).
			Background(Secondary).
			Padding(0, 1).
			Bold(true)

	DefaultTag = lipgloss.NewStyle().
			Foreground(lipgloss.Color("#1e1e2e")).
			Background(Primary).
			Padding(0, 1).
			Bold(true)
)

// ---- Lifecycle Icons (ABP pattern: ●◐○✓✗▪) ----

const (
	IconActive    = "●"
	IconPartial   = "◐"
	IconIdle      = "○"
	IconDone      = "✓"
	IconFailed    = "✗"
	IconQueued    = "▪"
	IconStreaming = "◉"
)

func LifecycleIcon(status string) string {
	switch status {
	case "active", "running":
		return lipgloss.NewStyle().Foreground(Secondary).Render(IconActive)
	case "streaming":
		return lipgloss.NewStyle().Foreground(Primary).Render(IconStreaming)
	case "idle", "paused":
		return lipgloss.NewStyle().Foreground(Warning).Render(IconPartial)
	case "completed", "done":
		return lipgloss.NewStyle().Foreground(Muted).Render(IconDone)
	case "failed", "error":
		return lipgloss.NewStyle().Foreground(Danger).Render(IconFailed)
	case "queued", "pending":
		return lipgloss.NewStyle().Foreground(Muted).Render(IconIdle)
	default:
		return lipgloss.NewStyle().Foreground(Muted).Render(IconIdle)
	}
}

// CategoryDot returns a colored dot for agent/conversation categories.
func CategoryDot(cat string) string {
	switch cat {
	case "coding":
		return lipgloss.NewStyle().Foreground(Secondary).Render("●")
	case "research":
		return lipgloss.NewStyle().Foreground(Warning).Render("●")
	case "infrastructure":
		return lipgloss.NewStyle().Foreground(Info).Render("●")
	default:
		return lipgloss.NewStyle().Foreground(Primary).Render("●")
	}
}
