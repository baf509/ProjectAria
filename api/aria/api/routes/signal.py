"""
ARIA - Signal Routes

Purpose: Manage Signal service lifecycle and message sending.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from motor.motor_asyncio import AsyncIOMotorDatabase

from aria.api.deps import get_db, get_orchestrator, get_signal_service
from aria.core.orchestrator import Orchestrator
from aria.signal.service import SignalService

router = APIRouter(prefix="/signal", tags=["signal"])


class SignalPairRequest(BaseModel):
    phone_number: str


class SignalSendRequest(BaseModel):
    phone_number: str
    message: str


class SignalInboundRequest(BaseModel):
    sender: str
    message: str = ""
    attachments: list[dict] = []


class SignalPollRequest(BaseModel):
    interval_seconds: int | None = None


@router.post("/start")
async def start_signal(service: SignalService = Depends(get_signal_service)):
    try:
        return await service.start()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to start Signal service: {exc}")


@router.post("/stop")
async def stop_signal(service: SignalService = Depends(get_signal_service)):
    return await service.stop()


@router.get("/status")
async def signal_status(service: SignalService = Depends(get_signal_service)):
    return await service.status()


@router.post("/pair")
async def pair_signal_sender(
    body: SignalPairRequest,
    service: SignalService = Depends(get_signal_service),
):
    try:
        return await service.add_allowed_sender(body.phone_number)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/send")
async def send_signal_message(
    body: SignalSendRequest,
    service: SignalService = Depends(get_signal_service),
):
    try:
        return await service.send(body.phone_number, body.message)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to send Signal message: {exc}")


@router.post("/inbound")
async def handle_inbound_signal_message(
    body: SignalInboundRequest,
    service: SignalService = Depends(get_signal_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    try:
        return await service.handle_incoming_text(
            sender=body.sender,
            message=body.message,
            attachments=body.attachments,
            db=db,
            orchestrator=orchestrator,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to handle inbound Signal message: {exc}")


@router.post("/poll")
async def poll_signal_messages(
    service: SignalService = Depends(get_signal_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    try:
        return await service.poll_once(db=db, orchestrator=orchestrator)
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to poll Signal messages: {exc}")


@router.post("/poll/start")
async def start_signal_polling(
    body: SignalPollRequest,
    service: SignalService = Depends(get_signal_service),
    db: AsyncIOMotorDatabase = Depends(get_db),
    orchestrator: Orchestrator = Depends(get_orchestrator),
):
    try:
        return await service.start_polling(
            db=db,
            orchestrator=orchestrator,
            interval_seconds=body.interval_seconds,
        )
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Failed to start Signal polling: {exc}")
