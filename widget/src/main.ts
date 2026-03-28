/**
 * ARIA Widget - Main entry point
 *
 * Manages conversation state and wires up UI components to the API.
 */

import {
  type Agent,
  createConversation,
  getConversation,
  listConversations,
  listAgents,
  streamMessage,
  switchConversationMode,
  setApiUrl,
  getApiUrl,
  setApiKey,
  getApiKey,
} from "./api";
import { init as initInput, setDisabled, focus } from "./components/ChatInput";
import {
  appendMessage,
  appendStreamingMessage,
  updateStreamingMessage,
  finalizeStreamingMessage,
  clearMessages,
  showEmptyState,
  renderMessages,
} from "./components/MessageList";

let conversationId: string | null = null;
let agents: Agent[] = [];

// --- Settings ---

const STORAGE_KEY = "aria-widget-settings";

interface Settings {
  apiUrl: string;
  apiKey: string;
}

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return { apiUrl: "http://corsair-ai.tailb286a5.ts.net:8000", apiKey: "" };
}

function saveSettings(s: Settings) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(s));
}

// --- Settings Panel ---

const settingsPanel = document.getElementById("settings-panel")!;
const btnSettings = document.getElementById("btn-settings")!;
const btnSaveSettings = document.getElementById("btn-save-settings")!;
const settingApiUrl = document.getElementById(
  "setting-api-url"
) as HTMLInputElement;
const settingApiKey = document.getElementById(
  "setting-api-key"
) as HTMLInputElement;

btnSettings.addEventListener("click", () => {
  settingsPanel.classList.toggle("hidden");
  if (!settingsPanel.classList.contains("hidden")) {
    settingApiUrl.value = getApiUrl();
    settingApiKey.value = getApiKey();
  }
});

btnSaveSettings.addEventListener("click", () => {
  const url = settingApiUrl.value.trim();
  const key = settingApiKey.value.trim();
  if (url) {
    setApiUrl(url);
    setApiKey(key);
    saveSettings({ apiUrl: url, apiKey: key });
  }
  settingsPanel.classList.add("hidden");
});

// --- Quick Actions ---

const btnQuickActions = document.getElementById("btn-quick-actions")!;
const quickActionsPanel = document.getElementById("quick-actions-panel")!;
const qaResearchInput = document.querySelector(".qa-research-input")!;
const qaResearchQuery = document.getElementById(
  "qa-research-query"
) as HTMLInputElement;

btnQuickActions.addEventListener("click", () => {
  quickActionsPanel.classList.toggle("hidden");
  // Hide settings if open
  settingsPanel.classList.add("hidden");
  // Reset research input when toggling
  if (quickActionsPanel.classList.contains("hidden")) {
    qaResearchInput.classList.add("hidden");
  }
});

document.querySelectorAll(".quick-action").forEach((btn) => {
  btn.addEventListener("click", () => {
    const action = (btn as HTMLElement).dataset.action;

    if (action === "research") {
      qaResearchInput.classList.toggle("hidden");
      if (!qaResearchInput.classList.contains("hidden")) {
        qaResearchQuery.value = "";
        qaResearchQuery.focus();
      }
      return;
    }

    quickActionsPanel.classList.add("hidden");
    qaResearchInput.classList.add("hidden");

    if (action === "coding-status") {
      handleSubmit("/coding-status");
    } else if (action === "memories") {
      handleSubmit("what do you remember?");
    } else if (action === "new-chat") {
      startNewChat();
    }
  });
});

qaResearchQuery.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    const query = qaResearchQuery.value.trim();
    if (query) {
      quickActionsPanel.classList.add("hidden");
      qaResearchInput.classList.add("hidden");
      handleSubmit(`/research ${query}`);
    }
  } else if (e.key === "Escape") {
    qaResearchInput.classList.add("hidden");
  }
});

// --- New Chat ---

const btnNewChat = document.getElementById("btn-new-chat")!;
const modePicker = document.getElementById("mode-picker") as HTMLSelectElement;

btnNewChat.addEventListener("click", startNewChat);
modePicker.addEventListener("change", handleModeChange);

async function startNewChat() {
  conversationId = null;
  syncModePicker(null);
  clearMessages();
  showEmptyState();
  focus();
}

// Expose for tray menu
(window as any).__ariaNewChat = startNewChat;

// --- Window animations and positioning ---

const appEl = document.getElementById("app")!;

function getTauriWindow() {
  try {
    const { getCurrentWindow } = (window as any).__TAURI__?.window ?? {};
    return getCurrentWindow?.() ?? null;
  } catch {
    return null;
  }
}

function getTauriInvoke() {
  return (window as any).__TAURI__?.core?.invoke ?? null;
}

/** Reposition the window to bottom-right, growing upward */
function repositionWindow() {
  const invoke = getTauriInvoke();
  if (!invoke) return;
  const height = Math.max(300, Math.min(document.documentElement.scrollHeight, 700));
  invoke("reposition_window", { height }).catch(() => {});
}

/** Animate show */
(window as any).__ariaAnimateShow = () => {
  appEl.classList.remove("animate-hide");
  appEl.classList.add("animate-show");
  repositionWindow();
  focus();
};

/** Animate hide, then actually hide the window */
(window as any).__ariaAnimateHide = () => {
  appEl.classList.remove("animate-show");
  appEl.classList.add("animate-hide");
  const win = getTauriWindow();
  let hidden = false;
  const doHide = () => {
    if (hidden) return;
    hidden = true;
    appEl.classList.remove("animate-hide");
    win?.hide();
  };
  appEl.addEventListener("animationend", doHide, { once: true });
  // Fallback in case animationend doesn't fire
  setTimeout(doHide, 200);
};

// Reposition whenever content changes height (debounced to prevent feedback loop)
let resizeTimer: ReturnType<typeof setTimeout> | null = null;
let lastHeight = 0;
const resizeObserver = new ResizeObserver(() => {
  const h = appEl.scrollHeight;
  if (Math.abs(h - lastHeight) < 5) return; // ignore sub-pixel jitter
  lastHeight = h;
  if (resizeTimer) clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => repositionWindow(), 100);
});
resizeObserver.observe(appEl);

// Initial reposition on first show
repositionWindow();

// --- Escape to hide ---

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    (window as any).__ariaAnimateHide?.();
  }
});

// --- Message handling ---

function renderModeOptions() {
  const currentValue = modePicker.value;
  modePicker.innerHTML = '<option value="">Mode</option>';

  for (const agent of agents) {
    const option = document.createElement("option");
    option.value = agent.slug;
    option.textContent = `${agent.mode_metadata?.icon ? `${agent.mode_metadata.icon} ` : ""}${agent.name}`;
    modePicker.appendChild(option);
  }

  if (currentValue) {
    modePicker.value = currentValue;
  }

  modePicker.disabled = agents.length === 0;
}

function syncModePicker(activeAgentId: string | null | undefined) {
  if (!activeAgentId) {
    modePicker.value = "";
    return;
  }

  const selected = agents.find((agent) => agent.id === activeAgentId);
  modePicker.value = selected?.slug || "";
}

async function loadAgentsForWidget() {
  try {
    agents = await listAgents();
    renderModeOptions();
  } catch (err: any) {
    appendMessage("error", err.message || "Failed to load modes");
  }
}

async function handleModeChange() {
  const agentSlug = modePicker.value;
  if (!conversationId || !agentSlug) return;

  try {
    const updated = await switchConversationMode(conversationId, agentSlug);
    syncModePicker(updated.active_agent_id ?? updated.agent_id ?? null);
    renderMessages(updated.messages ?? []);
  } catch (err: any) {
    appendMessage("error", err.message || "Failed to switch mode");
  }
}

async function ensureConversation(): Promise<string> {
  if (conversationId) return conversationId;

  // Try to resume last conversation
  try {
    const conversations = await listConversations(1);
    if (conversations.length > 0) {
      conversationId = conversations[0].id;
      const full = await getConversation(conversationId);
      syncModePicker(full.active_agent_id ?? full.agent_id ?? null);
      if (full.messages?.length > 0) {
        renderMessages(full.messages);
      }
      return conversationId;
    }
  } catch {}

  // Create new
  const convo = await createConversation("Quick Chat");
  conversationId = convo.id;
  syncModePicker(convo.active_agent_id ?? convo.agent_id ?? null);
  return conversationId;
}

async function handleSubmit(message: string) {
  setDisabled(true);

  // Clear empty state
  const emptyState = document.querySelector(".empty-state");
  if (emptyState) emptyState.remove();

  // Show user message
  appendMessage("user", message);

  try {
    const convId = await ensureConversation();

    // Start streaming assistant response
    const streamEl = appendStreamingMessage();
    let fullContent = "";

    for await (const chunk of streamMessage(convId, message)) {
      if (chunk.type === "text" && chunk.content) {
        fullContent += chunk.content;
        updateStreamingMessage(streamEl, fullContent);
      } else if (chunk.type === "error") {
        finalizeStreamingMessage(streamEl, "");
        streamEl.remove();
        appendMessage("error", chunk.error || "Unknown error");
        break;
      } else if (chunk.type === "done") {
        finalizeStreamingMessage(streamEl, fullContent);
        break;
      }
    }
  } catch (err: any) {
    appendMessage("error", err.message || "Connection failed");
  }

  setDisabled(false);
  focus();
}

// --- Init ---

const settings = loadSettings();
setApiUrl(settings.apiUrl);
setApiKey(settings.apiKey);
void loadAgentsForWidget();
initInput(handleSubmit);
showEmptyState();
