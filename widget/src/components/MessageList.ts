/**
 * MessageList component - renders chat messages in a scrollable container.
 */

import type { Message } from "../api";
import { synthesizeSpeech } from "../api";

const container = document.getElementById("messages")!;

export function appendMessage(
  role: "user" | "assistant" | "error",
  content: string
): HTMLElement {
  const el = document.createElement("div");
  el.className = `message ${role}`;
  el.textContent = content;
  container.appendChild(el);
  scrollToBottom();
  return el;
}

export function appendStreamingMessage(): HTMLElement {
  const el = document.createElement("div");
  el.className = "message assistant";
  el.innerHTML = '<span class="cursor"></span>';
  container.appendChild(el);
  scrollToBottom();
  return el;
}

export function updateStreamingMessage(el: HTMLElement, content: string) {
  el.innerHTML = content + '<span class="cursor"></span>';
  scrollToBottom();
}

export function finalizeStreamingMessage(el: HTMLElement, content: string) {
  el.textContent = content;
  addTtsButton(el, content);
}

let currentAudio: HTMLAudioElement | null = null;
let currentPlayBtn: HTMLButtonElement | null = null;

function addTtsButton(messageEl: HTMLElement, text: string) {
  const btn = document.createElement("button");
  btn.className = "tts-play-btn";
  btn.innerHTML = "&#x1f50a;";
  btn.title = "Read aloud";

  btn.addEventListener("click", async () => {
    // If this button is already playing, stop it
    if (currentAudio && currentPlayBtn === btn) {
      currentAudio.pause();
      currentAudio = null;
      currentPlayBtn = null;
      btn.innerHTML = "&#x1f50a;";
      btn.classList.remove("playing");
      return;
    }

    // Stop any other playing audio
    if (currentAudio) {
      currentAudio.pause();
      currentAudio = null;
      if (currentPlayBtn) {
        currentPlayBtn.innerHTML = "&#x1f50a;";
        currentPlayBtn.classList.remove("playing");
      }
    }

    btn.innerHTML = "&#x23f3;";
    btn.classList.add("loading");

    try {
      const wavBuffer = await synthesizeSpeech(text);
      const blob = new Blob([wavBuffer], { type: "audio/wav" });
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);

      btn.innerHTML = "&#x23f9;";
      btn.classList.remove("loading");
      btn.classList.add("playing");

      currentAudio = audio;
      currentPlayBtn = btn;

      audio.addEventListener("ended", () => {
        URL.revokeObjectURL(url);
        btn.innerHTML = "&#x1f50a;";
        btn.classList.remove("playing");
        currentAudio = null;
        currentPlayBtn = null;
      });

      audio.play();
    } catch {
      btn.innerHTML = "&#x1f50a;";
      btn.classList.remove("loading");
    }
  });

  messageEl.appendChild(btn);
}

export function clearMessages() {
  container.innerHTML = "";
}

export function showEmptyState() {
  container.innerHTML =
    '<div class="empty-state">Press Ctrl+Space to toggle.<br>Type a message to start chatting.</div>';
}

export function renderMessages(messages: Message[]) {
  clearMessages();
  for (const msg of messages) {
    if (msg.role === "user" || msg.role === "assistant") {
      const el = appendMessage(msg.role, msg.content);
      if (msg.role === "assistant") {
        addTtsButton(el, msg.content);
      }
    }
  }
}

function scrollToBottom() {
  container.scrollTop = container.scrollHeight;
}
