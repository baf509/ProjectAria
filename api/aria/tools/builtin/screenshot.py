"""
ARIA - Screenshot Analysis Tool

Purpose: Capture screenshot and analyze via multimodal LLM.
"""

import asyncio
import base64
import logging
import os
import tempfile

from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.config import settings
from aria.llm.manager import llm_manager

logger = logging.getLogger(__name__)


class ScreenshotTool(BaseTool):
    """Capture a screenshot and analyze it with a vision-capable LLM."""

    @property
    def name(self) -> str:
        return "screenshot_analyze"

    @property
    def description(self) -> str:
        return (
            "Capture a screenshot of the current display and analyze it "
            "using a vision-capable LLM. Returns a text description of what's on screen."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="query",
                type="string",
                description="What to look for or describe in the screenshot",
                required=False,
                default="Describe what's on the screen.",
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        query = arguments.get("query", "Describe what's on the screen.")

        # Check for display availability
        if not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"):
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="No display available (DISPLAY/WAYLAND_DISPLAY not set). "
                      "Cannot capture screenshot in headless/Docker environments.",
            )

        # Capture screenshot
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = tmp.name

            cmd = settings.screenshot_command
            process = await asyncio.create_subprocess_exec(
                cmd, tmp_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10)

            if process.returncode != 0:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error=f"Screenshot command failed: {stderr.decode().strip()}",
                )

            # Read and base64 encode
            with open(tmp_path, "rb") as f:
                image_data = f.read()

            if not image_data:
                return ToolResult(
                    tool_name=self.name,
                    status=ToolStatus.ERROR,
                    error="Screenshot file is empty",
                )

            image_b64 = base64.b64encode(image_data).decode("utf-8")

        except FileNotFoundError:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Screenshot command '{settings.screenshot_command}' not found. "
                      f"Install it or configure screenshot_command in settings.",
            )
        except asyncio.TimeoutError:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="Screenshot capture timed out",
            )
        except Exception as exc:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Screenshot capture failed: {exc}",
            )
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

        # Send to vision LLM
        try:
            backend = settings.screenshot_vision_backend
            model = settings.screenshot_vision_model
            adapter = llm_manager.get_adapter(backend, model)

            # Build multimodal message
            from aria.llm.base import Message
            messages = [
                Message(
                    role="user",
                    content=[
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": image_b64,
                            },
                        },
                        {"type": "text", "text": query},
                    ],
                )
            ]

            analysis_parts = []
            async for chunk in adapter.stream(
                messages, temperature=0.3, max_tokens=1024, stream=False
            ):
                if chunk.type == "text":
                    analysis_parts.append(chunk.content)

            analysis = "".join(analysis_parts).strip()
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output=analysis,
                metadata={"image_size_bytes": len(image_data), "backend": backend, "model": model},
            )

        except Exception as exc:
            logger.error("Screenshot analysis failed: %s", exc, exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Vision analysis failed: {exc}",
            )
