package components

import (
	"encoding/json"
	"fmt"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// DBBrowser lets you explore MongoDB collections and documents.
type DBBrowser struct {
	Width  int
	Height int

	// State
	collections []api.CollectionInfo
	queryResult *api.QueryResult
	document    map[string]interface{}

	// Navigation
	mode       dbMode // collections list, documents list, document detail
	cursor     int
	offset     int
	collection string // currently selected collection
	docIndex   int    // selected doc in results
	page       int

	// Input
	filterInput string
	editing     bool
}

type dbMode int

const (
	dbModeCollections dbMode = iota
	dbModeDocuments
	dbModeDetail
)

func NewDBBrowser() *DBBrowser {
	return &DBBrowser{mode: dbModeCollections}
}

func (d *DBBrowser) SetSize(w, h int) {
	d.Width = w
	d.Height = h
}

func (d *DBBrowser) SetCollections(cols []api.CollectionInfo) {
	d.collections = cols
	d.mode = dbModeCollections
	d.cursor = 0
	d.offset = 0
}

func (d *DBBrowser) SetQueryResult(result *api.QueryResult) {
	d.queryResult = result
	d.mode = dbModeDocuments
	d.cursor = 0
	d.offset = 0
}

func (d *DBBrowser) SetDocument(doc map[string]interface{}) {
	d.document = doc
	d.mode = dbModeDetail
	d.offset = 0
}

func (d *DBBrowser) Update(msg tea.Msg) (*DBBrowser, tea.Cmd) {
	return d, nil
}

func (d *DBBrowser) Focus() {
	d.editing = false
}

func (d *DBBrowser) Blur() {
	d.editing = false
}

// SelectedCollection returns the collection name at cursor when in collections mode.
func (d *DBBrowser) SelectedCollection() string {
	if d.mode != dbModeCollections || len(d.collections) == 0 {
		return ""
	}
	if d.cursor >= 0 && d.cursor < len(d.collections) {
		return d.collections[d.cursor].Name
	}
	return ""
}

// SelectedDocID returns the _id of the selected document in documents mode.
func (d *DBBrowser) SelectedDocID() string {
	if d.mode != dbModeDocuments || d.queryResult == nil {
		return ""
	}
	if d.cursor >= 0 && d.cursor < len(d.queryResult.Documents) {
		doc := d.queryResult.Documents[d.cursor]
		if id, ok := doc["_id"]; ok {
			return docIDString(id)
		}
	}
	return ""
}

func (d *DBBrowser) CurrentCollection() string {
	return d.collection
}

func (d *DBBrowser) SetCollection(name string) {
	d.collection = name
}

func (d *DBBrowser) Mode() dbMode {
	return d.mode
}

func (d *DBBrowser) Page() int {
	return d.page
}

func (d *DBBrowser) SetPage(p int) {
	d.page = p
}

func (d *DBBrowser) GetFilter() string {
	return d.filterInput
}

func (d *DBBrowser) SetFilter(f string) {
	d.filterInput = f
}

func (d *DBBrowser) IsEditing() bool {
	return d.editing
}

func (d *DBBrowser) ToggleEditing() {
	d.editing = !d.editing
}

func (d *DBBrowser) HandleFilterKey(key string) {
	switch key {
	case "backspace":
		if len(d.filterInput) > 0 {
			d.filterInput = d.filterInput[:len(d.filterInput)-1]
		}
	default:
		if len(key) == 1 {
			d.filterInput += key
		}
	}
}

// GoBack returns to the previous level.
func (d *DBBrowser) GoBack() bool {
	switch d.mode {
	case dbModeDetail:
		d.mode = dbModeDocuments
		d.document = nil
		return true
	case dbModeDocuments:
		d.mode = dbModeCollections
		d.queryResult = nil
		d.collection = ""
		d.filterInput = ""
		d.page = 0
		return true
	}
	return false
}

func (d *DBBrowser) Up() {
	if d.cursor > 0 {
		d.cursor--
	}
	if d.cursor < d.offset {
		d.offset = d.cursor
	}
}

func (d *DBBrowser) Down() {
	maxIdx := d.maxItems() - 1
	if d.cursor < maxIdx {
		d.cursor++
	}
	visible := d.visibleLines()
	if d.cursor >= d.offset+visible {
		d.offset = d.cursor - visible + 1
	}
}

func (d *DBBrowser) ScrollUp() {
	if d.offset > 0 {
		d.offset--
	}
}

func (d *DBBrowser) ScrollDown() {
	d.offset++
}

func (d *DBBrowser) maxItems() int {
	switch d.mode {
	case dbModeCollections:
		return len(d.collections)
	case dbModeDocuments:
		if d.queryResult != nil {
			return len(d.queryResult.Documents)
		}
	}
	return 0
}

func (d *DBBrowser) visibleLines() int {
	return max(1, d.Height-8)
}

func (d *DBBrowser) View() string {
	if d.Width < 10 || d.Height < 5 {
		return ""
	}

	var content string
	switch d.mode {
	case dbModeCollections:
		content = d.renderCollections()
	case dbModeDocuments:
		content = d.renderDocuments()
	case dbModeDetail:
		content = d.renderDetail()
	}

	border := styles.PaneBorder
	return border.Width(d.Width - 2).Height(d.Height - 2).Render(content)
}

func (d *DBBrowser) renderCollections() string {
	var b strings.Builder

	title := styles.PanelTitle.Render(" Database Browser")
	b.WriteString(title + "\n")
	b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
		fmt.Sprintf(" %d collections", len(d.collections))) + "\n\n")

	visible := d.visibleLines()
	end := d.offset + visible
	if end > len(d.collections) {
		end = len(d.collections)
	}

	for i := d.offset; i < end; i++ {
		col := d.collections[i]
		name := col.Name
		count := lipgloss.NewStyle().Foreground(styles.Muted).Render(fmt.Sprintf(" (%d)", col.Count))
		line := " " + name + count

		if i == d.cursor {
			b.WriteString(styles.SidebarSelected.Render(line) + "\n")
		} else {
			b.WriteString(styles.SidebarItem.Render(line) + "\n")
		}
	}

	return b.String()
}

func (d *DBBrowser) renderDocuments() string {
	var b strings.Builder

	title := styles.PanelTitle.Render(" " + d.collection)
	b.WriteString(title)
	if d.queryResult != nil {
		info := lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf("  %d documents (page %d)", d.queryResult.Total, d.page+1))
		b.WriteString(info)
	}
	b.WriteString("\n")

	// Filter line
	filterLabel := styles.HelpKey.Render(" /") + styles.HelpDesc.Render(" filter: ")
	if d.editing {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Accent).Render(d.filterInput + "_")
	} else if d.filterInput != "" {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Text).Render(d.filterInput)
	} else {
		filterLabel += lipgloss.NewStyle().Foreground(styles.Muted).Render("(none)")
	}
	b.WriteString(filterLabel + "\n\n")

	if d.queryResult == nil || len(d.queryResult.Documents) == 0 {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  No documents") + "\n")
		return b.String()
	}

	contentWidth := d.Width - 8
	visible := d.visibleLines()
	end := d.offset + visible
	if end > len(d.queryResult.Documents) {
		end = len(d.queryResult.Documents)
	}

	for i := d.offset; i < end; i++ {
		doc := d.queryResult.Documents[i]
		line := d.formatDocLine(doc, contentWidth)

		if i == d.cursor {
			b.WriteString(styles.SidebarSelected.Render(line) + "\n")
		} else {
			b.WriteString(styles.SidebarItem.Render(line) + "\n")
		}
	}

	return b.String()
}

func (d *DBBrowser) renderDetail() string {
	var b strings.Builder

	title := styles.PanelTitle.Render(" Document Detail")
	b.WriteString(title + "\n\n")

	if d.document == nil {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render("  No document selected") + "\n")
		return b.String()
	}

	// Pretty-print JSON
	jsonBytes, err := json.MarshalIndent(d.document, "", "  ")
	if err != nil {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Danger).Render("  Error formatting document") + "\n")
		return b.String()
	}

	lines := strings.Split(string(jsonBytes), "\n")
	visible := d.visibleLines() + 2
	end := d.offset + visible
	if end > len(lines) {
		end = len(lines)
	}
	start := d.offset
	if start > len(lines) {
		start = len(lines)
	}

	for i := start; i < end; i++ {
		line := lines[i]
		// Syntax coloring
		colored := colorizeJSON(line)
		b.WriteString(" " + colored + "\n")
	}

	if len(lines) > visible {
		pct := 0
		if len(lines)-visible > 0 {
			pct = d.offset * 100 / (len(lines) - visible)
		}
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(
			fmt.Sprintf("\n line %d/%d (%d%%)", d.offset+1, len(lines), pct)))
	}

	return b.String()
}

func (d *DBBrowser) formatDocLine(doc map[string]interface{}, maxWidth int) string {
	id := docIDString(doc["_id"])
	if len(id) > 24 {
		id = id[:24]
	}

	// Try to show a useful summary field
	summary := ""
	for _, key := range []string{"title", "name", "content", "slug", "summary", "prompt", "sender", "status"} {
		if v, ok := doc[key]; ok && v != nil {
			s := fmt.Sprintf("%v", v)
			if len(s) > 0 {
				summary = s
				break
			}
		}
	}

	idStr := lipgloss.NewStyle().Foreground(styles.Accent).Render(id)
	avail := maxWidth - len(id) - 3
	if avail < 0 {
		avail = 10
	}
	if len(summary) > avail {
		summary = summary[:avail-1] + "…"
	}

	if summary != "" {
		return " " + idStr + " " + summary
	}
	return " " + idStr
}

func docIDString(id interface{}) string {
	if id == nil {
		return "?"
	}
	switch v := id.(type) {
	case string:
		return v
	case map[string]interface{}:
		if oid, ok := v["$oid"]; ok {
			return fmt.Sprintf("%v", oid)
		}
	}
	return fmt.Sprintf("%v", id)
}

func colorizeJSON(line string) string {
	trimmed := strings.TrimSpace(line)
	indent := line[:len(line)-len(trimmed)]

	// Keys (quoted strings followed by colon)
	if strings.Contains(trimmed, ":") {
		parts := strings.SplitN(trimmed, ":", 2)
		key := strings.TrimSpace(parts[0])
		val := ""
		if len(parts) > 1 {
			val = strings.TrimSpace(parts[1])
		}
		coloredKey := lipgloss.NewStyle().Foreground(styles.Accent).Render(key)
		coloredVal := colorizeValue(val)
		return indent + coloredKey + ": " + coloredVal
	}

	return indent + colorizeValue(trimmed)
}

func colorizeValue(val string) string {
	val = strings.TrimRight(val, ",")
	trailing := ""
	if strings.HasSuffix(strings.TrimSpace(val)+",", val+",") {
		// check original had comma
	}

	switch {
	case val == "null":
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("null")
	case val == "true":
		return lipgloss.NewStyle().Foreground(styles.Secondary).Render("true")
	case val == "false":
		return lipgloss.NewStyle().Foreground(styles.Warning).Render("false")
	case strings.HasPrefix(val, "\""):
		return lipgloss.NewStyle().Foreground(styles.Text).Render(val) + trailing
	case val == "{" || val == "}" || val == "[" || val == "]" || val == "{}" || val == "[]":
		return lipgloss.NewStyle().Foreground(styles.Overlay).Render(val)
	default:
		// Numbers
		return lipgloss.NewStyle().Foreground(styles.Warning).Render(val) + trailing
	}
}
