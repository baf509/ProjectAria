/**
 * ARIA API Client
 *
 * Connects to the ProjectAria backend API for conversations and streaming.
 */

const DEFAULT_API_URL = "http://corsair-ai.tailb286a5.ts.net:8000";

export interface Conversation {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  messages: Message[];
}

export interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  timestamp?: string;
}

export interface StreamChunk {
  type: "text" | "tool_call" | "done" | "error";
  content?: string;
  error?: string;
}

let apiUrl = DEFAULT_API_URL;

export function setApiUrl(url: string) {
  apiUrl = url.replace(/\/+$/, "");
}

export function getApiUrl(): string {
  return apiUrl;
}

async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const res = await fetch(`${apiUrl}/api/v1${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
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

/**
 * Send a message and stream the response via SSE.
 * Yields StreamChunk objects as they arrive.
 */
export async function* streamMessage(
  conversationId: string,
  content: string
): AsyncGenerator<StreamChunk> {
  const res = await fetch(
    `${apiUrl}/api/v1/conversations/${conversationId}/messages`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
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

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";

    for (const line of lines) {
      if (line.startsWith("data: ")) {
        const data = line.slice(6).trim();
        if (!data || data === "[DONE]") continue;
        try {
          const chunk: StreamChunk = JSON.parse(data);
          yield chunk;
        } catch {
          // Skip malformed chunks
        }
      }
    }
  }
}

export async function checkHealth(): Promise<{ status: string }> {
  const res = await apiFetch("/health");
  return res.json();
}

export async function synthesizeSpeech(
  text: string,
  speaker = "Ryan",
  language = "English",
  instruct?: string
): Promise<ArrayBuffer> {
  const res = await fetch(`${apiUrl}/api/v1/tts/synthesize`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
    body: formData,
  });
  if (!res.ok) {
    throw new Error(`STT error ${res.status}: ${res.statusText}`);
  }
  return res.json();
}
