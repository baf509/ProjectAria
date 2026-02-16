"""
ARIA - STT Proxy Routes

Phase: 6
Purpose: Proxy requests to the STT microservice

Related Spec Sections:
- Section 6: Voice Input
"""

import httpx
from fastapi import APIRouter, File, HTTPException, UploadFile

from aria.config import settings

router = APIRouter(prefix="/stt", tags=["stt"])

STT_TIMEOUT = 120.0  # CPU inference can be slow


@router.post("/transcribe")
async def transcribe(
    file: UploadFile = File(...),
    language: str | None = None,
):
    """Transcribe an audio file to text via the STT service."""
    audio_bytes = await file.read()

    async with httpx.AsyncClient(timeout=STT_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{settings.stt_url}/stt/transcribe",
                files={"file": (file.filename or "audio.wav", audio_bytes, file.content_type or "audio/wav")},
                data={"language": language} if language else {},
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="STT service unavailable")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="STT transcription timed out")

    if resp.status_code != 200:
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return resp.json()


@router.get("/health")
async def stt_health():
    """Check STT service health."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{settings.stt_url.replace('/v1', '')}/health")
        except (httpx.ConnectError, httpx.TimeoutException):
            return {"status": "unavailable"}

    return resp.json()
