"""
ARIA - TTS Proxy Routes

Phase: 6
Purpose: Proxy requests to the TTS microservice

Related Spec Sections:
- Section 6: Voice Output
"""

import httpx
from fastapi import APIRouter, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel

from aria.config import settings

router = APIRouter(prefix="/tts", tags=["tts"])

TTS_TIMEOUT = 120.0  # CPU inference can be slow


class SynthesizeRequest(BaseModel):
    text: str
    speaker: str = "Ryan"
    language: str = "English"
    instruct: str | None = None


@router.post("/synthesize")
async def synthesize(request: SynthesizeRequest) -> Response:
    """Synthesize speech from text via the TTS service."""
    async with httpx.AsyncClient(timeout=TTS_TIMEOUT) as client:
        try:
            resp = await client.post(
                f"{settings.tts_url}/tts/synthesize",
                json=request.model_dump(exclude_none=True),
            )
        except httpx.ConnectError:
            raise HTTPException(status_code=503, detail="TTS service unavailable")
        except httpx.TimeoutException:
            raise HTTPException(status_code=504, detail="TTS synthesis timed out")

    if resp.status_code != 200:
        detail = resp.text
        try:
            detail = resp.json().get("detail", detail)
        except Exception:
            pass
        raise HTTPException(status_code=resp.status_code, detail=detail)

    return Response(content=resp.content, media_type="audio/wav")


@router.get("/speakers")
async def list_speakers():
    """List available TTS speakers."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{settings.tts_url}/tts/speakers")
        except (httpx.ConnectError, httpx.TimeoutException):
            raise HTTPException(status_code=503, detail="TTS service unavailable")

    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail="Failed to list speakers")

    return resp.json()


@router.get("/health")
async def tts_health():
    """Check TTS service health."""
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            base_url = settings.tts_url.rsplit("/v1", 1)[0]
            resp = await client.get(f"{base_url}/health")
            if resp.status_code != 200:
                return {"status": "unhealthy", "status_code": resp.status_code}
            return resp.json()
        except (httpx.ConnectError, httpx.TimeoutException):
            return {"status": "unavailable"}
        except Exception:
            return {"status": "error"}
