"""
ARIA - Task Routes

Purpose: Background task inspection and cancellation.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from aria.api.deps import get_task_runner
from aria.tasks.runner import TaskRunner

router = APIRouter(prefix="/tasks", tags=["tasks"])


@router.get("")
async def list_tasks(
    status: str | None = None,
    runner: TaskRunner = Depends(get_task_runner),
):
    return await runner.list_tasks(status=status)


@router.get("/{task_id}")
async def get_task(
    task_id: str,
    runner: TaskRunner = Depends(get_task_runner),
):
    task = await runner.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.post("/{task_id}/cancel")
async def cancel_task(
    task_id: str,
    runner: TaskRunner = Depends(get_task_runner),
):
    success = await runner.cancel_task(task_id)
    if not success:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"task_id": task_id, "cancelled": True}
