"""
ARIA - Text-to-Speech Microservice

Serves Qwen3-TTS speech synthesis via a simple REST API.
Runs on CPU using the 0.6B CustomVoice model.
"""

import asyncio
import io
from contextlib import asynccontextmanager

import numpy as np
import soundfile as sf
import torch
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from qwen_tts import Qwen3TTSModel

MODEL_NAME = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"

SPEAKERS = [
    "Vivian", "Serena", "Uncle_Fu", "Dylan", "Eric",
    "Ryan", "Aiden", "Ono_Anna", "Sohee",
]

tts_model: Qwen3TTSModel | None = None

MAX_TEXT_LENGTH = 5000  # Characters — limit to avoid extremely long synthesis times


@asynccontextmanager
async def lifespan(app: FastAPI):
    global tts_model
    tts_model = Qwen3TTSModel.from_pretrained(
        MODEL_NAME,
        device_map="cpu",
        dtype=torch.float32,
    )
    yield
    tts_model = None


app = FastAPI(title="ARIA TTS", lifespan=lifespan)


class SynthesizeRequest(BaseModel):
    text: str
    speaker: str = "Ryan"
    language: str = "English"
    instruct: str | None = None


def _synthesize(request: SynthesizeRequest) -> tuple[bytes, int]:
    """Run TTS inference (blocking, meant to be called via to_thread)."""
    if tts_model is None:
        raise RuntimeError("Model not loaded")

    if request.speaker not in SPEAKERS:
        raise ValueError(f"Unknown speaker: {request.speaker}. Available: {SPEAKERS}")

    kwargs = {
        "text": request.text,
        "speaker": request.speaker,
        "language": request.language,
    }
    if request.instruct:
        kwargs["instruct"] = request.instruct

    wavs, sr = tts_model.generate_custom_voice(**kwargs)

    # wavs may be a list of tensors or a single tensor
    if isinstance(wavs, list):
        audio = wavs[0]
    else:
        audio = wavs

    if isinstance(audio, torch.Tensor):
        audio = audio.cpu().numpy()

    # Ensure 1D
    audio = np.squeeze(audio)

    # Encode as WAV
    buf = io.BytesIO()
    sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue(), sr


@app.post("/v1/tts/synthesize")
async def synthesize(request: SynthesizeRequest) -> Response:
    if tts_model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    if not request.text.strip():
        raise HTTPException(status_code=400, detail="Text cannot be empty")

    if len(request.text) > MAX_TEXT_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"Text too long ({len(request.text)} chars). Maximum is {MAX_TEXT_LENGTH}.",
        )

    try:
        wav_bytes, _ = await asyncio.to_thread(_synthesize, request)
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
        "speakers": len(SPEAKERS),
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8002)
