"""
ARIA - Text-to-Speech Microservice

Serves Qwen3-TTS speech synthesis via a simple REST API.
Runs on CPU using the 0.6B CustomVoice model.
"""

import asyncio
import io
from contextlib import asynccontextmanager

import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from qwen_tts import QwenTTS

MODEL_NAME = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
SAMPLE_RATE = 24000

SPEAKERS = [
    "Chelsie", "Ethan", "Ryan", "Layla", "Luke",
    "Natasha", "Oliver", "Sophia", "Tyler",
]

tts_model: QwenTTS | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts_model
    tts_model = QwenTTS.from_pretrained(
        MODEL_NAME,
        device_map="cpu",
        torch_dtype=torch.float32,
    )
    yield
    tts_model = None


app = FastAPI(title="ARIA TTS", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    speaker: str = "Chelsie"
    language: str = "English"
    instruct: str | None = None


def _synthesize(request: SynthesizeRequest) -> bytes:
    """Run TTS inference (blocking, meant to be called via to_thread)."""
    if tts_model is None:
        raise RuntimeError("Model not loaded")

    if request.speaker not in SPEAKERS:
        raise ValueError(f"Unknown speaker: {request.speaker}. Available: {SPEAKERS}")

    spk_text = f"[{request.speaker}]: "
    instruct_text = request.instruct or f"Speak naturally in {request.language}."

    audio = tts_model.synthesize(
        text=request.text,
        speaker=spk_text,
        instruct=instruct_text,
    )

    # Encode as WAV
    buf = io.BytesIO()
    sf.write(buf, audio.cpu().numpy(), SAMPLE_RATE, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.post("/v1/tts/synthesize")
async def synthesize(request: SynthesizeRequest) -> Response:
    if tts_model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    try:
        wav_bytes = await asyncio.to_thread(_synthesize, request)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Synthesis failed: {e}")

    return Response(content=wav_bytes, media_type="audio/wav")


@app.get("/v1/tts/speakers")
async def list_speakers():
    return {"speakers": SPEAKERS}


@app.get("/health")
async def health():
    return {
        "status": "healthy" if tts_model is not None else "loading",
        "model": MODEL_NAME,
        "sample_rate": SAMPLE_RATE,
        "speakers": len(SPEAKERS),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
