package components

import (
	"fmt"
	"strconv"
	"strings"

	"github.com/ben/aria-tui/internal/api"
	"github.com/ben/aria-tui/internal/ui/styles"
	"github.com/charmbracelet/lipgloss"
)

// ModelsView is the cockpit's model-server screen: pick WHICH model to load,
// and choose HOW it loads.
//
// The second half is the reason this screen exists rather than a list with a
// start button. This box runs the same model several ways — UD-IQ3_XXS on the
// Strix Halo iGPU alone, UD-IQ3_S split across the iGPU and the R9700, an
// affine quant on the sealed ROCm runtime — and the choice between them is
// device placement, KV type, context and drafter, not just a slug. Those are
// the deployment's own env knobs; ARIA applies them as a systemd drop-in, so
// picking one here is the same act as editing the file by hand.
//
// Two consequences shape the layout:
//   - Every parameter shows where its current value came from. ARIA's own
//     override, a hand-written drop-in and a serve.sh default are all "the
//     current value", but only the first is ARIA's to clear.
//   - Memory is shown PER POOL. Two GPUs with separate memory means a model on
//     the R9700 does not compete with one on the Halo — running one of each is
//     a supported deployment, and one combined number would hide that.
type ModelsView struct {
	Width   int
	Height  int
	Focused bool

	Servers []api.ModelServer

	cursor     int  // selected server
	paramFocus bool // true when the parameter pane has focus
	paramIdx   int
	editing    bool
	editBuf    string

	// Pending overrides for the selected server, keyed by parameter name.
	// Cleared whenever the selection changes so a value typed against one
	// model can never be submitted against another.
	draft    map[string]string
	draftFor string

	Status string // last action result, shown in the footer
}

func NewModelsView() *ModelsView {
	return &ModelsView{draft: map[string]string{}}
}

func (mv *ModelsView) SetSize(w, h int) { mv.Width, mv.Height = w, h }
func (mv *ModelsView) Focus()           { mv.Focused = true }
func (mv *ModelsView) Blur()            { mv.Focused = false }

func (mv *ModelsView) SetData(servers []api.ModelServer) {
	mv.Servers = servers
	if mv.cursor >= len(servers) {
		mv.cursor = 0
	}
	mv.clampParam()
}

// Selected returns the highlighted server, or nil when the list is empty.
func (mv *ModelsView) Selected() *api.ModelServer {
	if mv.cursor < 0 || mv.cursor >= len(mv.Servers) {
		return nil
	}
	return &mv.Servers[mv.cursor]
}

func (mv *ModelsView) MoveCursor(d int) {
	if len(mv.Servers) == 0 {
		return
	}
	if mv.paramFocus {
		mv.moveParam(d)
		return
	}
	mv.cursor = (mv.cursor + d + len(mv.Servers)) % len(mv.Servers)
	mv.paramIdx = 0
	mv.resetDraft()
}

func (mv *ModelsView) moveParam(d int) {
	s := mv.Selected()
	if s == nil || len(s.Parameters) == 0 {
		return
	}
	mv.paramIdx = (mv.paramIdx + d + len(s.Parameters)) % len(s.Parameters)
}

func (mv *ModelsView) clampParam() {
	s := mv.Selected()
	if s == nil || mv.paramIdx >= len(s.Parameters) {
		mv.paramIdx = 0
	}
}

// ToggleParamFocus moves focus between the model list and its parameters.
// Refused for a server with no selectable parameters, so focus never lands
// somewhere with nothing to do.
func (mv *ModelsView) ToggleParamFocus() {
	s := mv.Selected()
	if s == nil || len(s.Parameters) == 0 {
		mv.paramFocus = false
		return
	}
	mv.paramFocus = !mv.paramFocus
	mv.editing = false
}

func (mv *ModelsView) resetDraft() {
	mv.draft = map[string]string{}
	mv.draftFor = ""
	mv.editing = false
	mv.editBuf = ""
}

// Value is the value that would be launched: a pending edit if there is one,
// otherwise whatever the server currently reports.
func (mv *ModelsView) value(p api.LaunchParam) string {
	if v, ok := mv.draft[p.Name]; ok {
		return v
	}
	return p.Value
}

// CycleChoice steps an enum parameter through its declared options. Enums are
// cycled rather than typed because their values are exact — "Vulkan1" vs
// "Vulkan0" is the difference between the right card and a 78 GiB spill over
// OCuLink.
func (mv *ModelsView) CycleChoice(d int) {
	s := mv.Selected()
	if s == nil || !mv.paramFocus || mv.paramIdx >= len(s.Parameters) {
		return
	}
	p := s.Parameters[mv.paramIdx]
	if len(p.Choices) == 0 {
		return
	}
	cur := mv.value(p)
	idx := 0
	for i, c := range p.Choices {
		if c.Value == cur {
			idx = i
			break
		}
	}
	next := p.Choices[(idx+d+len(p.Choices))%len(p.Choices)].Value
	mv.setDraft(s.Slug, p.Name, next)
}

func (mv *ModelsView) setDraft(slug, name, value string) {
	if mv.draftFor != slug {
		mv.draft = map[string]string{}
		mv.draftFor = slug
	}
	mv.draft[name] = value
}

// BeginEdit starts inline editing of a free-form parameter (context size,
// drafter path). Enum parameters are cycled instead and are not editable.
func (mv *ModelsView) BeginEdit() bool {
	s := mv.Selected()
	if s == nil || !mv.paramFocus || mv.paramIdx >= len(s.Parameters) {
		return false
	}
	p := s.Parameters[mv.paramIdx]
	if p.Kind == "enum" {
		mv.CycleChoice(1)
		return true
	}
	mv.editing = true
	mv.editBuf = mv.value(p)
	return true
}

func (mv *ModelsView) IsEditing() bool { return mv.editing }

// HandleEditKey consumes a keystroke while inline editing. Returns false for
// keys it does not own, so the caller can fall through to screen actions.
func (mv *ModelsView) HandleEditKey(key string) bool {
	if !mv.editing {
		return false
	}
	s := mv.Selected()
	if s == nil || mv.paramIdx >= len(s.Parameters) {
		mv.editing = false
		return false
	}
	p := s.Parameters[mv.paramIdx]
	switch key {
	case "esc":
		mv.editing = false
		mv.editBuf = ""
	case "enter":
		mv.setDraft(s.Slug, p.Name, strings.TrimSpace(mv.editBuf))
		mv.editing = false
	case "backspace":
		if n := len(mv.editBuf); n > 0 {
			mv.editBuf = mv.editBuf[:n-1]
		}
	default:
		if len(key) == 1 {
			mv.editBuf += key
		}
	}
	return true
}

// Overrides is what to send with a start: the pending edits, plus any value
// ARIA already owns so that re-starting does not silently drop it.
func (mv *ModelsView) Overrides() map[string]string {
	s := mv.Selected()
	if s == nil {
		return nil
	}
	out := map[string]string{}
	for _, p := range s.Parameters {
		v := mv.value(p)
		if v == "" {
			continue
		}
		if _, edited := mv.draft[p.Name]; edited || p.Source == "aria_override" {
			out[p.Name] = v
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func (mv *ModelsView) ClearDraft() { mv.resetDraft() }

// ---------------------------------------------------------------- rendering

func poolLabel(pool string) string {
	switch pool {
	case "halo-gtt":
		return "Halo"
	case "r9700-vram":
		return "R9700"
	case "host-ram":
		return "CPU"
	case "remote":
		return "remote"
	}
	return pool
}

func sourceLabel(source string) string {
	switch source {
	case "aria_override":
		return "set here"
	case "unit_dropin":
		return "drop-in"
	case "script_default":
		return "script"
	case "declared_default":
		return "default"
	}
	return source
}

func stateStyle(state string) (lipgloss.Style, string) {
	switch state {
	case "running":
		return lipgloss.NewStyle().Foreground(styles.Secondary), "●"
	case "restarting", "starting":
		return lipgloss.NewStyle().Foreground(styles.Warning), "◐"
	case "dead":
		return lipgloss.NewStyle().Foreground(styles.Danger), "●"
	}
	return lipgloss.NewStyle().Foreground(styles.Muted), "○"
}

// poolSummary collapses the server rows into one line per GPU pool. Read from
// the rows rather than fetched separately so it survives a partial response.
func (mv *ModelsView) poolSummary() string {
	seen := map[string]bool{}
	var parts []string
	for _, s := range mv.Servers {
		if s.PoolTotalGiB == nil || seen[s.MemoryPool] {
			continue
		}
		if s.MemoryPool == "host-ram" || s.MemoryPool == "remote" {
			continue
		}
		seen[s.MemoryPool] = true
		used := 0.0
		if s.PoolUsedGiB != nil {
			used = *s.PoolUsedGiB
		}
		part := fmt.Sprintf("%s %.0f/%.0f GiB", poolLabel(s.MemoryPool), used, *s.PoolTotalGiB)
		if s.PoolSpilling {
			part += " SPILLING"
		}
		parts = append(parts, part)
	}
	return strings.Join(parts, "   ")
}

func (mv *ModelsView) renderList(width, height int) string {
	var b strings.Builder
	b.WriteString(styles.SectionTitle.Render(" Models") + "\n")

	// Window the list around the cursor so a long registry still shows the
	// selection on a short terminal.
	start := 0
	if height > 0 && mv.cursor >= height {
		start = mv.cursor - height + 1
	}
	for i := start; i < len(mv.Servers) && i-start < height; i++ {
		s := mv.Servers[i]
		st, dot := stateStyle(s.State)
		name := truncate(s.Slug, width-14)
		line := fmt.Sprintf(" %s %-*s %5s", st.Render(dot), width-14, name, poolLabel(s.MemoryPool))
		if i == mv.cursor {
			style := lipgloss.NewStyle().Foreground(styles.Primary)
			if !mv.paramFocus {
				style = style.Bold(true)
			}
			b.WriteString(style.Render("›" + line))
		} else if !s.Startable {
			// Unstartable entries stay visible: their reason is the record of
			// what happened to that deployment, and hiding them invites
			// re-adding a model that is already here.
			b.WriteString(lipgloss.NewStyle().Foreground(styles.Muted).Render(" " + line))
		} else {
			b.WriteString(" " + line)
		}
		b.WriteString("\n")
	}
	return b.String()
}

func (mv *ModelsView) renderDetail(width int) string {
	s := mv.Selected()
	if s == nil {
		return lipgloss.NewStyle().Foreground(styles.Muted).Render("  No model servers.")
	}
	var b strings.Builder
	muted := lipgloss.NewStyle().Foreground(styles.Muted)
	sub := lipgloss.NewStyle().Foreground(styles.SubText)

	b.WriteString(styles.SectionTitle.Render(" "+truncate(s.Slug, width-2)) + "\n")

	devices := strings.Join(s.Devices, " + ")
	if devices == "" {
		devices = s.BackendDevice
	}
	b.WriteString(sub.Render("  "+truncate(devices, width-4)) + "\n")

	mem := fmt.Sprintf("  %s pool", poolLabel(s.MemoryPool))
	for _, extra := range s.AlsoUses {
		mem += " + " + poolLabel(extra)
	}
	if s.ResidentGiBMeasured != nil {
		mem += fmt.Sprintf("   %.1f GiB resident (measured)", *s.ResidentGiBMeasured)
	} else if s.ResidentGiBEstimate > 0 {
		mem += fmt.Sprintf("   ~%.1f GiB (projected)", s.ResidentGiBEstimate)
	}
	if s.ServedCtx != nil && *s.ServedCtx > 0 {
		slots := 1
		if s.Slots != nil && *s.Slots > 0 {
			slots = *s.Slots
		}
		mem += fmt.Sprintf("   %d ctx x %d", *s.ServedCtx, slots)
	}
	b.WriteString(muted.Render(mem) + "\n")

	if !s.Startable && s.NotStartableReason != "" {
		b.WriteString(lipgloss.NewStyle().Foreground(styles.Warning).
			Render("  ! "+truncate(s.NotStartableReason, width-6)) + "\n")
	}
	b.WriteString("\n")

	if len(s.Parameters) == 0 {
		b.WriteString(muted.Render("  Launch configuration is frozen in this deployment's") + "\n")
		b.WriteString(muted.Render("  compose file or unit — no selectable parameters.") + "\n")
		return b.String()
	}

	title := " Launch configuration"
	if mv.paramFocus {
		title += "  (tab: back to list)"
	} else {
		title += "  (tab: edit)"
	}
	b.WriteString(styles.SectionTitle.Render(title) + "\n")

	for i, p := range s.Parameters {
		val := mv.value(p)
		src := sourceLabel(p.Source)
		if _, edited := mv.draft[p.Name]; edited {
			src = "pending"
		}
		if mv.editing && mv.paramFocus && i == mv.paramIdx {
			val = mv.editBuf + "_"
			src = "editing"
		}

		marker := "  "
		nameStyle := lipgloss.NewStyle().Foreground(styles.SubText)
		if mv.paramFocus && i == mv.paramIdx {
			marker = "› "
			nameStyle = lipgloss.NewStyle().Foreground(styles.Primary).Bold(true)
		}
		valStyle := lipgloss.NewStyle()
		if src == "pending" || src == "editing" {
			valStyle = valStyle.Foreground(styles.Warning)
		} else if p.Source == "aria_override" {
			valStyle = valStyle.Foreground(styles.Secondary)
		}
		b.WriteString(fmt.Sprintf("%s%s %s %s\n",
			marker,
			nameStyle.Render(fmt.Sprintf("%-14s", truncate(p.Label, 14))),
			valStyle.Render(fmt.Sprintf("%-28s", truncate(val, 28))),
			muted.Render("("+src+")")))

		// The selected option's own note carries the measured trade-off (what
		// fits, what it costs in decode), which is the thing worth reading.
		if mv.paramFocus && i == mv.paramIdx {
			for _, c := range p.Choices {
				if c.Value == val && c.Description != "" {
					b.WriteString(muted.Render("      "+truncate(c.Description, width-8)) + "\n")
					break
				}
			}
		}
	}
	return b.String()
}

func (mv *ModelsView) View() string {
	if mv.Width < 20 || mv.Height < 6 {
		return ""
	}

	listW := mv.Width / 3
	if listW < 24 {
		listW = 24
	}
	if listW > 40 {
		listW = 40
	}
	detailW := mv.Width - listW - 6
	bodyH := mv.Height - 7
	if bodyH < 3 {
		bodyH = 3
	}

	left := lipgloss.NewStyle().Width(listW).Render(mv.renderList(listW, bodyH))
	right := lipgloss.NewStyle().Width(detailW).Render(mv.renderDetail(detailW))
	body := lipgloss.JoinHorizontal(lipgloss.Top, left, right)

	header := styles.TitleStyle.Render("Model Servers")
	pools := lipgloss.NewStyle().Foreground(styles.Muted).Render("  " + mv.poolSummary())

	help := "  s: start with settings │ d: start with defaults │ x: stop │ tab: parameters │ r: refresh │ Esc: back"
	if mv.paramFocus {
		help = "  ↑/↓: parameter │ ←/→: choose │ enter: edit │ s: start with settings │ tab: list │ Esc: back"
	}
	if mv.editing {
		help = "  type a value │ enter: keep │ esc: cancel"
	}
	footer := lipgloss.NewStyle().Foreground(styles.Muted).Render(help)
	if mv.Status != "" {
		footer = lipgloss.NewStyle().Foreground(styles.Warning).Render("  "+truncate(mv.Status, mv.Width-6)) +
			"\n" + footer
	}

	content := lipgloss.JoinVertical(lipgloss.Left, header, pools, "", body, footer)

	border := styles.PaneBorder
	if mv.Focused {
		border = styles.PaneBorderActive
	}
	return border.Width(mv.Width - 2).Height(mv.Height - 2).Render(content)
}

// ParamSummary renders the pending launch choice as a one-line string, for the
// confirmation the screen shows after a start.
func (mv *ModelsView) ParamSummary() string {
	over := mv.Overrides()
	if len(over) == 0 {
		return "deployment defaults"
	}
	keys := make([]string, 0, len(over))
	for k := range over {
		keys = append(keys, k)
	}
	// Stable order so the summary does not shuffle between renders.
	for i := 0; i < len(keys); i++ {
		for j := i + 1; j < len(keys); j++ {
			if keys[j] < keys[i] {
				keys[i], keys[j] = keys[j], keys[i]
			}
		}
	}
	parts := make([]string, 0, len(keys))
	for _, k := range keys {
		parts = append(parts, k+"="+over[k])
	}
	return strings.Join(parts, " ")
}

// ValidateDraft catches the obvious mistakes before a round trip. The API
// validates authoritatively; this only saves a pointless request.
func (mv *ModelsView) ValidateDraft() error {
	s := mv.Selected()
	if s == nil {
		return nil
	}
	for _, p := range s.Parameters {
		v, edited := mv.draft[p.Name]
		if !edited {
			continue
		}
		if p.Kind == "int" {
			if _, err := strconv.Atoi(v); err != nil {
				return fmt.Errorf("%s must be a number, got %q", p.Label, v)
			}
		}
	}
	return nil
}
