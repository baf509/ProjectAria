/**
 * ChatInput component - handles text input, submission, and voice recording.
 */

import { transcribeSpeech } from "../api";

export type OnSubmit = (message: string) => void;

const form = document.getElementById("input-form") as HTMLFormElement;
const input = document.getElementById("input") as HTMLTextAreaElement;
const sendBtn = document.getElementById("btn-send") as HTMLButtonElement;
const micBtn = document.getElementById("btn-mic") as HTMLButtonElement;

let onSubmitHandler: OnSubmit | null = null;
let disabled = false;

// --- Voice recording state ---
let mediaRecorder: MediaRecorder | null = null;
let recordingChunks: Blob[] = [];
let isRecording = false;

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

  // Mic button
  micBtn.addEventListener("click", toggleRecording);
}

function submit() {
  if (disabled) return;
  const text = input.value.trim();
  if (!text || !onSubmitHandler) return;

  onSubmitHandler(text);
  input.value = "";
  input.style.height = "auto";
}

async function toggleRecording() {
  if (disabled) return;

  if (isRecording) {
    stopRecording();
  } else {
    await startRecording();
  }
}

async function startRecording() {
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    recordingChunks = [];

    mediaRecorder = new MediaRecorder(stream, { mimeType: "audio/webm" });

    mediaRecorder.addEventListener("dataavailable", (e) => {
      if (e.data.size > 0) {
        recordingChunks.push(e.data);
      }
    });

    mediaRecorder.addEventListener("stop", async () => {
      // Stop all tracks to release the microphone
      stream.getTracks().forEach((t) => t.stop());

      const audioBlob = new Blob(recordingChunks, { type: "audio/webm" });
      recordingChunks = [];

      if (audioBlob.size === 0) return;

      // Show transcribing state
      micBtn.classList.add("transcribing");
      micBtn.title = "Transcribing...";

      try {
        const result = await transcribeSpeech(audioBlob);
        if (result.text.trim()) {
          // Insert transcribed text into the input
          input.value =
            input.value + (input.value ? " " : "") + result.text.trim();
          input.dispatchEvent(new Event("input")); // trigger auto-resize
          input.focus();
        }
      } catch {
        // Silently fail — user can retry
      } finally {
        micBtn.classList.remove("transcribing");
        micBtn.title = "Voice input";
      }
    });

    mediaRecorder.start();
    isRecording = true;
    micBtn.classList.add("recording");
    micBtn.title = "Stop recording";
  } catch {
    // Microphone permission denied or unavailable
    micBtn.title = "Microphone unavailable";
  }
}

function stopRecording() {
  if (mediaRecorder && mediaRecorder.state !== "inactive") {
    mediaRecorder.stop();
  }
  isRecording = false;
  micBtn.classList.remove("recording");
}

export function setDisabled(value: boolean) {
  disabled = value;
  input.disabled = value;
  sendBtn.disabled = value;
  micBtn.disabled = value;
}

export function focus() {
  input.focus();
}
