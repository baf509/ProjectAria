/**
 * MessageList component - renders chat messages in a scrollable container.
 */

import type { Message } from "../api";

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
      appendMessage(msg.role, msg.content);
    }
  }
}

function scrollToBottom() {
  container.scrollTop = container.scrollHeight;
}
