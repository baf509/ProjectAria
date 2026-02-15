/**
 * ChatInput component - handles text input and submission.
 */

export type OnSubmit = (message: string) => void;

const form = document.getElementById("input-form") as HTMLFormElement;
const input = document.getElementById("input") as HTMLTextAreaElement;
const sendBtn = document.getElementById("btn-send") as HTMLButtonElement;

let onSubmitHandler: OnSubmit | null = null;
let disabled = false;

export function init(onSubmit: OnSubmit) {
  onSubmitHandler = onSubmit;

  form.addEventListener("submit", (e) => {
    e.preventDefault();
    submit();
  });

  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      submit();
    }
  });

  // Auto-resize textarea
  input.addEventListener("input", () => {
    input.style.height = "auto";
    input.style.height = Math.min(input.scrollHeight, 120) + "px";
  });
}

function submit() {
  if (disabled) return;
  const text = input.value.trim();
  if (!text || !onSubmitHandler) return;

  onSubmitHandler(text);
  input.value = "";
  input.style.height = "auto";
}

export function setDisabled(value: boolean) {
  disabled = value;
  input.disabled = value;
  sendBtn.disabled = value;
}

export function focus() {
  input.focus();
}
