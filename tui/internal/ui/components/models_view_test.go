package components

import (
	"strings"
	"testing"

	"github.com/ben/aria-tui/internal/api"
)

func sampleServers() []api.ModelServer {
	halo := 97.0
	total := 124.0
	return []api.ModelServer{
		{
			Slug:       "DS4-Halo",
			State:      "running",
			MemoryPool: "halo-gtt",
			Devices:    []string{"Strix Halo iGPU (Vulkan1)"},
			Startable:  true, Onbox: true,
			PoolUsedGiB: &halo, PoolTotalGiB: &total,
			Parameters: []api.LaunchParam{
				{
					Name: "kv", Env: "KV", Label: "KV type", Kind: "enum",
					Value: "f16", Source: "script_default",
					Choices: []api.LaunchChoice{
						{Value: "f16", Description: "65536 ctx"},
						{Value: "q8_0", Description: "131072 ctx"},
					},
				},
				{Name: "ctx", Env: "CTX", Label: "Context", Kind: "int", Value: "65536", Source: "script_default"},
			},
		},
		{
			Slug:       "Qwen-R9700",
			State:      "exited",
			MemoryPool: "r9700-vram",
			Startable:  true, Onbox: true,
			Parameters: []api.LaunchParam{
				{Name: "ctx", Env: "CTX", Label: "Context", Kind: "int", Value: "65536", Source: "unit_dropin"},
			},
		},
		{
			Slug: "Frozen-Compose", State: "not_created", MemoryPool: "r9700-vram",
			Startable: true, Onbox: true,
		},
	}
}

func newLoadedView() *ModelsView {
	mv := NewModelsView()
	mv.SetSize(120, 30)
	mv.SetData(sampleServers())
	return mv
}

func TestOverridesEmptyUntilSomethingIsChosen(t *testing.T) {
	// A start with no explicit choice must send nothing, because "no overrides"
	// is what tells the API to clear a previous session's settings.
	mv := newLoadedView()
	if got := mv.Overrides(); got != nil {
		t.Fatalf("expected no overrides before any edit, got %v", got)
	}
	if s := mv.ParamSummary(); s != "deployment defaults" {
		t.Fatalf("unexpected summary %q", s)
	}
}

func TestCycleChoiceRequiresParameterFocus(t *testing.T) {
	mv := newLoadedView()
	mv.CycleChoice(1) // list has focus — must be inert
	if mv.Overrides() != nil {
		t.Fatal("cycling without parameter focus changed the launch config")
	}
	mv.ToggleParamFocus()
	mv.CycleChoice(1)
	over := mv.Overrides()
	if over["kv"] != "q8_0" {
		t.Fatalf("expected kv=q8_0 after one cycle, got %v", over)
	}
	mv.CycleChoice(1) // wraps back
	if mv.Overrides()["kv"] != "f16" {
		t.Fatal("expected the choice list to wrap")
	}
}

func TestSelectingAnotherModelDiscardsPendingEdits(t *testing.T) {
	// A value typed against one model must never be submitted against another.
	mv := newLoadedView()
	mv.ToggleParamFocus()
	mv.CycleChoice(1)
	if mv.Overrides() == nil {
		t.Fatal("precondition: expected a pending override")
	}
	mv.ToggleParamFocus() // back to the list
	mv.MoveCursor(1)
	if got := mv.Overrides(); got != nil {
		t.Fatalf("pending edits survived a selection change: %v", got)
	}
}

func TestInlineEditingCapturesDigitsAndCommitsOnEnter(t *testing.T) {
	mv := newLoadedView()
	mv.ToggleParamFocus()
	mv.moveParam(1) // the int parameter
	mv.BeginEdit()
	if !mv.IsEditing() {
		t.Fatal("expected edit mode")
	}
	mv.editBuf = ""
	for _, k := range []string{"1", "3", "1", "0", "7", "2"} {
		if !mv.HandleEditKey(k) {
			t.Fatalf("edit mode did not consume %q", k)
		}
	}
	mv.HandleEditKey("enter")
	if mv.IsEditing() {
		t.Fatal("enter should commit and leave edit mode")
	}
	if got := mv.Overrides()["ctx"]; got != "131072" {
		t.Fatalf("expected ctx=131072, got %q", got)
	}
}

func TestEscapeAbandonsAnEditWithoutChangingAnything(t *testing.T) {
	mv := newLoadedView()
	mv.ToggleParamFocus()
	mv.moveParam(1)
	mv.BeginEdit()
	mv.HandleEditKey("9")
	mv.HandleEditKey("esc")
	if mv.IsEditing() {
		t.Fatal("esc should leave edit mode")
	}
	if mv.Overrides() != nil {
		t.Fatal("an abandoned edit must not become an override")
	}
}

func TestEditKeysAreNotConsumedOutsideEditMode(t *testing.T) {
	// Otherwise "s" (start) would be swallowed into a text buffer.
	mv := newLoadedView()
	if mv.HandleEditKey("s") {
		t.Fatal("keys must fall through to screen actions when not editing")
	}
}

func TestValidateDraftCatchesNonNumericContext(t *testing.T) {
	mv := newLoadedView()
	mv.ToggleParamFocus()
	mv.moveParam(1)
	mv.BeginEdit()
	mv.editBuf = "lots"
	mv.HandleEditKey("enter")
	if err := mv.ValidateDraft(); err == nil {
		t.Fatal("expected a validation error for a non-numeric context size")
	}
}

func TestParameterFocusRefusedWhenThereIsNothingToConfigure(t *testing.T) {
	mv := newLoadedView()
	mv.MoveCursor(2) // the compose-frozen entry
	mv.ToggleParamFocus()
	if mv.paramFocus {
		t.Fatal("focus moved into an empty parameter pane")
	}
	view := mv.View()
	if !strings.Contains(view, "frozen") {
		t.Fatalf("expected the frozen-configuration explanation, got:\n%s", view)
	}
}

func TestPoolSummaryReportsEachDeviceSeparately(t *testing.T) {
	// One combined number would hide that a model on the R9700 does not
	// compete with one on the Halo.
	mv := newLoadedView()
	summary := mv.poolSummary()
	if !strings.Contains(summary, "Halo 97/124 GiB") {
		t.Fatalf("expected the Halo pool in the summary, got %q", summary)
	}
}

func TestViewRendersSourceAttributionForEachParameter(t *testing.T) {
	mv := newLoadedView()
	view := mv.View()
	if !strings.Contains(view, "script") {
		t.Fatalf("expected a value's source to be shown, got:\n%s", view)
	}
}
