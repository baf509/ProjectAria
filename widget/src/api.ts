/**
 * ARIA API Client
 *
 * Connects to the ProjectAria backend API for conversations and streaming.
 */

const DEFAULT_API_URL = "http://localhost:8000";

export interface Conversation {
  id: string;
  agent_id?: string;
  active_agent_id?: string | null;
  title: string;
  created_at: string;
  updated_at: string;
  messages: Message[];
}

export interface Agent {
  id: string;
  name: string;
  slug: string;
  mode_metadata?: {
    icon?: string | null;
    color?: string | null;
    keywords?: string[];
  } | null;
}

export interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: string;
}

export interface StreamChunk {
  type: "text" | "tool_call" | "done" | "error";
  event_id?: string;
  content?: string;
  error?: string;
}

let apiUrl = DEFAULT_API_URL;
let apiKey = "";

export function setApiUrl(url: string) {
  apiUrl = url.replace(/\/+$/, "");
}

export function getApiUrl(): string {
  return apiUrl;
}

export function setApiKey(key: string) {
  apiKey = key;
}

export function getApiKey(): string {
  return apiKey;
}

function authHeaders(): Record<string, string> {
  return apiKey ? { "X-API-Key": apiKey } : {};
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${apiUrl}/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...authHeaders(),
      ...init?.headers,
    },
  });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${res.statusText}`);
  }
  return res;
}

export async function createConversation(
  title = "Quick Chat"
): Promise<Conversation> {
  const res = await apiFetch("/conversations", {
    method: "POST",
    body: JSON.stringify({ title }),
  });
  return res.json();
}

export async function listConversations(
  limit = 20
): Promise<Conversation[]> {
  const res = await apiFetch(`/conversations?limit=${limit}`);
  return res.json();
}

export async function getConversation(id: string): Promise<Conversation> {
  const res = await apiFetch(`/conversations/${id}`);
  return res.json();
}

export async function listAgents(): Promise<Agent[]> {
  const res = await apiFetch("/agents");
  return res.json();
}

export async function switchConversationMode(
  conversationId: string,
  agentSlug: string
): Promise<Conversation> {
  const res = await apiFetch(`/conversations/${conversationId}/switch-mode`, {
    method: "POST",
    body: JSON.stringify({ agent_slug: agentSlug }),
  });
  return res.json();
}

/**
 * Send a message and stream the response via SSE.
 * Yields StreamChunk objects as they arrive.
 */
export async function* streamMessage(
  conversationId: string,
  content: string,
  lastEventId?: string
): AsyncGenerator<StreamChunk> {
  const res = await fetch(
    `${apiUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...authHeaders(),
        ...(lastEventId ? { "Last-Event-ID": lastEventId } : {}),
      },
      body: JSON.stringify({ content, stream: true }),
    }
  );

  if (!res.ok) {
    yield { type: "error", error: `API error ${res.status}: ${res.statusText}` };
    return;
  }

  const reader = res.body?.getReader();
  if (!reader) {
    yield { type: "error", error: "No response body" };
    return;
  }

  const decoder = new TextDecoder();
  let buffer = "";
  let currentEvent: { id?: string; event?: string; data: string[] } = { data: [] };

  const flushEvent = async function* (): AsyncGenerator<StreamChunk> {
    if (currentEvent.data.length === 0) {
      return;
    }

    const eventMeta = currentEvent;
    const rawData = eventMeta.data.join("\n").trim();
    currentEvent = { data: [] };

    if (!rawData || rawData === "[DONE]") {
      return;
    }

    try {
      const parsed = JSON.parse(rawData) as StreamChunk;
      if (eventMeta.event && !parsed.type) {
        parsed.type = eventMeta.event as StreamChunk["type"];
      }
      if (eventMeta.id) {
        parsed.event_id = eventMeta.id;
      }
      yield parsed;
    } catch {
      yield { type: "error", error: "Malformed SSE payload" };
    }
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r?\n/);
      buffer = lines.pop() || "";

      for (const line of lines) {
        if (line === "") {
          for await (const chunk of flushEvent()) {
            yield chunk;
          }
          continue;
        }
        if (line.startsWith(":")) {
          continue;
        }
        if (line.startsWith("id:")) {
          currentEvent.id = line.slice(3).trim();
          continue;
        }
        if (line.startsWith("event:")) {
          currentEvent.event = line.slice(6).trim();
          continue;
        }
        if (line.startsWith("data:")) {
          currentEvent.data.push(line.slice(5).trimStart());
        }
      }
    }

    for await (const chunk of flushEvent()) {
      yield chunk;
    }
  } finally {
    reader.releaseLock();
  }
}

export async function checkHealth(): Promise<{ status: string }> {
  const res = await apiFetch("/health");
  return res.json();
}

export async function synthesizeSpeech(
  text: string,
  speaker = "Vivian",
  language = "English",
  instruct?: string
): Promise<ArrayBuffer> {
  const res = await fetch(`${apiUrl}/api/v1/tts/synthesize`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ text, speaker, language, instruct }),
  });
  if (!res.ok) {
    throw new Error(`TTS error ${res.status}: ${res.statusText}`);
  }
  return res.arrayBuffer();
}

export async function transcribeSpeech(
  audioBlob: Blob,
  language?: string
): Promise<{ text: string; language: string; duration: number }> {
  const formData = new FormData();
  formData.append("file", audioBlob, "recording.wav");
  if (language) {
    formData.append("language", language);
  }

  const res = await fetch(`${apiUrl}/api/v1/stt/transcribe`, {
    method: "POST",
    headers: { ...authHeaders() },
    body: formData,
  });
  if (!res.ok) {
    throw new Error(`STT error ${res.status}: ${res.statusText}`);
  }
  return res.json();
}
