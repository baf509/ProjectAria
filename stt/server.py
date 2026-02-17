"""
ARIA - Speech-to-Text Microservice

Serves transcription via faster-whisper with large-v3-turbo (CTranslate2).
Runs on CPU with int8 quantization for reasonable performance.
"""

import asyncio
import io
import tempfile
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, HTTPException, UploadFile
from faster_whisper import WhisperModel

MODEL_NAME = "large-v3-turbo"
MAX_AUDIO_SIZE = 25 * 1024 * 1024  # 25 MB

whisper_model: WhisperModel | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global whisper_model
    whisper_model = WhisperModel(
        MODEL_NAME,
        device="cpu",
        compute_type="int8",
    )
    yield
    whisper_model = None


app = FastAPI(title="ARIA STT", lifespan=lifespan)


def _transcribe(audio_bytes: bytes, language: str | None) -> dict:
    """Run Whisper inference (blocking, meant to be called via to_thread)."""
    if whisper_model is None:
        raise RuntimeError("Model not loaded")

    # Write to temp file — faster-whisper needs a file path
    # No suffix: audio may be webm, ogg, etc. — ffmpeg handles format detection
    with tempfile.NamedTemporaryFile(delete=True) as tmp:
        tmp.write(audio_bytes)
        tmp.flush()

        kwargs = {}
        if language:
            kwargs["language"] = language

        segments, info = whisper_model.transcribe(tmp.name, **kwargs)
        text = " ".join(seg.text.strip() for seg in segments)

    return {
        "text": text,
        "language": info.language,
        "language_probability": round(info.language_probability, 3),
        "duration": round(info.duration, 2),
    }


@app.post("/v1/stt/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = None,
):
    """Transcribe an audio file to text."""
    if whisper_model is None:
        raise HTTPException(status_code=503, detail="Model not loaded")

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=400, detail="Empty audio file")

    if len(audio_bytes) > MAX_AUDIO_SIZE:
        raise HTTPException(
            status_code=413,
            detail=f"Audio file too large ({len(audio_bytes)} bytes). Maximum is {MAX_AUDIO_SIZE}.",
        )

    try:
        result = await asyncio.to_thread(_transcribe, audio_bytes, language)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Transcription failed: {e}")

    return result


@app.get("/health")
async def health():
    return {
        "status": "healthy" if whisper_model is not None else "loading",
        "model": MODEL_NAME,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8003)
