/**
 * ARIA Widget - Main entry point
 *
 * Manages conversation state and wires up UI components to the API.
 */

import {
  createConversation,
  getConversation,
  listConversations,
  streamMessage,
  setApiUrl,
  getApiUrl,
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

// --- Settings ---

const STORAGE_KEY = "aria-widget-settings";

interface Settings {
  apiUrl: string;
}

function loadSettings(): Settings {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (raw) return JSON.parse(raw);
  } catch {}
  return { apiUrl: "http://corsair-ai.tailb286a5.ts.net:8000" };
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

btnSettings.addEventListener("click", () => {
  settingsPanel.classList.toggle("hidden");
  if (!settingsPanel.classList.contains("hidden")) {
    settingApiUrl.value = getApiUrl();
  }
});

btnSaveSettings.addEventListener("click", () => {
  const url = settingApiUrl.value.trim();
  if (url) {
    setApiUrl(url);
    saveSettings({ apiUrl: url });
  }
  settingsPanel.classList.add("hidden");
});

// --- New Chat ---

const btnNewChat = document.getElementById("btn-new-chat")!;

btnNewChat.addEventListener("click", startNewChat);

async function startNewChat() {
  conversationId = null;
  clearMessages();
  showEmptyState();
  focus();
}

// Expose for tray menu
(window as any).__ariaNewChat = startNewChat;

// --- Escape to hide ---

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    // Hide the window via Tauri API if available
    try {
      const { getCurrentWindow } = (window as any).__TAURI__?.window ?? {};
      if (getCurrentWindow) {
        getCurrentWindow().hide();
      }
    } catch {}
  }
});

// --- Message handling ---

async function ensureConversation(): Promise<string> {
  if (conversationId) return conversationId;

  // Try to resume last conversation
  try {
    const conversations = await listConversations(1);
    if (conversations.length > 0) {
      conversationId = conversations[0].id;
      const full = await getConversation(conversationId);
      if (full.messages?.length > 0) {
        renderMessages(full.messages);
      }
      return conversationId;
    }
  } catch {}

  // Create new
  const convo = await createConversation("Quick Chat");
  conversationId = convo.id;
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
initInput(handleSubmit);
showEmptyState();
