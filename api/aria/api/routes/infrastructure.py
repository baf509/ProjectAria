"""
ARIA - Infrastructure Routes

Purpose: Shared infrastructure management endpoints.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from aria.api.deps import get_model_switcher
from aria.infrastructure.model_switcher import LlamaCppModelSwitcher

router = APIRouter(prefix="/infrastructure", tags=["infrastructure"])


class SwitchModelRequest(BaseModel):
    model_name: str
    restart: bool = False


@router.get("/llamacpp/models")
async def list_llamacpp_models(
    switcher: LlamaCppModelSwitcher = Depends(get_model_switcher),
):
    return {"models": [model.to_dict() for model in await switcher.list_models()]}


@router.post("/llamacpp/models/switch")
async def switch_llamacpp_model(
    body: SwitchModelRequest,
    switcher: LlamaCppModelSwitcher = Depends(get_model_switcher),
):
    try:
        return await switcher.switch_model(body.model_name, restart=body.restart)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except RuntimeError as exc:
        raise HTTPException(status_code=500, detail=str(exc))
