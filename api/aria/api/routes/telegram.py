"""
ARIA - Telegram Routes

Purpose: API endpoints for Telegram bot status and control.
"""

from fastapi import APIRouter, Depends

from aria.api.deps import get_telegram_handler
from aria.telegram.handler import TelegramHandler

router = APIRouter()


@router.get("/telegram/status")
async def telegram_status(
    handler: TelegramHandler = Depends(get_telegram_handler),
):
    """Get Telegram bot status."""
    return handler.status()


@router.post("/telegram/start")
async def start_telegram(
    handler: TelegramHandler = Depends(get_telegram_handler),
):
    """Start Telegram polling (requires bot token configured)."""
    # Polling is started in lifespan; this is for manual restart
    return {"message": "Use lifespan startup or restart the service to start Telegram polling"}


@router.post("/telegram/stop")
async def stop_telegram(
    handler: TelegramHandler = Depends(get_telegram_handler),
):
    """Stop Telegram polling."""
    await handler.stop_polling()
    return {"stopped": True}
