"""
ARIA - Infrastructure Model Switcher

Purpose: Non-interactive access to the shared infrastructure llama.cpp model switch script.
"""

from __future__ import annotations

import asyncio
import os
from dataclasses import asdict, dataclass
from functools import partial
from typing import Optional

from aria.config import settings


@dataclass
class AvailableModel:
    name: str
    models_dir: str
    model_path: str
    backend: str
    active: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


class LlamaCppModelSwitcher:
    """Wrap the infrastructure switch-model.sh script for programmatic use."""

    def __init__(self):
        self.infrastructure_root = os.path.abspath(settings.infrastructure_root)
        self.script_path = os.path.join(self.infrastructure_root, "scripts", "switch-model.sh")
        self.models_dir = os.path.join(self.infrastructure_root, "models", "llm")
        self.env_file = os.path.join(self.infrastructure_root, ".env")

    async def list_models(self) -> list[AvailableModel]:
        current = self.get_current_model()
        models = []
        loop = asyncio.get_running_loop()
        entries = await loop.run_in_executor(None, partial(sorted, os.listdir(self.models_dir)))
        for name in entries:
            entry = os.path.join(self.models_dir, name)
            if os.path.isdir(entry):
                ggufs = sorted(
                    filename for filename in await loop.run_in_executor(None, os.listdir, entry)
                    if filename.endswith(".gguf")
                )
                if not ggufs:
                    continue
                model_path = f"/models/{ggufs[0]}"
                models_dir = f"./models/llm/{name}"
            elif name.endswith(".gguf"):
                model_path = f"/models/{name}"
                models_dir = "./models/llm"
            else:
                continue

            backend = "vulkan" if name.startswith("Qwen_Qwen3.5-") else "rocm"
            models.append(
                AvailableModel(
                    name=name,
                    models_dir=models_dir,
                    model_path=model_path,
                    backend=backend,
                    active=model_path == current,
                )
            )
        return models

    def get_current_model(self) -> Optional[str]:
        if not os.path.exists(self.env_file):
            return None
        with open(self.env_file, "r", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("LLAMACPP_MODEL="):
                    return line.split("=", 1)[1].strip()
        return None

    async def switch_model(self, model_name: str, restart: bool = False) -> dict:
        models = await self.list_models()
        selected_index = None
        for index, model in enumerate(models, start=1):
            if model.name == model_name or model.model_path == model_name:
                selected_index = index
                break
        if selected_index is None:
            raise ValueError(f"Unknown model: {model_name}")

        process = await asyncio.create_subprocess_exec(
            "bash",
            self.script_path,
            cwd=self.infrastructure_root,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        if process.stdin is None:
            raise RuntimeError("Failed to open stdin for switch-model.sh")
        stdin_data = f"{selected_index}\n{'y' if restart else 'n'}\n".encode("utf-8")
        stdout, stderr = await process.communicate(input=stdin_data)
        if process.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="replace") or stdout.decode("utf-8", errors="replace"))

        refreshed_models = await self.list_models()
        active = next((model for model in refreshed_models if model.active), None)
        return {
            "active_model": active.to_dict() if active else None,
            "restart_requested": restart,
            "stdout": stdout.decode("utf-8", errors="replace"),
        }
