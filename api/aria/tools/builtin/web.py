"""
ARIA - Web Tool

Phase: 3
Purpose: Built-in tool for fetching web content

Related Spec Sections:
- Section 8.3: Phase 3 - Tools & MCP

Features:
- HTTP GET requests
- Custom headers support
- Timeout configuration
- Response metadata (status, headers, etc.)
"""

import aiohttp
from typing import Optional
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


class WebTool(BaseTool):
    """
    Built-in tool for fetching web content.

    Supports:
    - HTTP/HTTPS GET requests
    - Custom headers
    - Timeout configuration
    - Response status and headers
    """

    def __init__(
        self,
        timeout_seconds: int = 30,
        max_response_size: int = 10 * 1024 * 1024,  # 10MB default
        user_agent: str = "ARIA/0.2.0",
    ):
        """
        Initialize web tool.

        Args:
            timeout_seconds: Request timeout in seconds
            max_response_size: Maximum response size in bytes
            user_agent: Default User-Agent header
        """
        super().__init__()
        self.timeout_seconds = timeout_seconds
        self.max_response_size = max_response_size
        self.user_agent = user_agent

        logger.info(
            f"Initialized WebTool with timeout={timeout_seconds}s, "
            f"max_size={max_response_size} bytes"
        )

    @property
    def name(self) -> str:
        return "web_fetch"

    @property
    def description(self) -> str:
        return (
            "Fetch content from a URL using HTTP GET. "
            "Returns the response body, status code, and headers."
        )

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(
                name="url",
                type="string",
                description="The URL to fetch",
                required=True,
            ),
            ToolParameter(
                name="headers",
                type="object",
                description="Custom HTTP headers as key-value pairs (optional)",
                required=False,
            ),
            ToolParameter(
                name="timeout",
                type="number",
                description="Request timeout in seconds (optional, overrides default)",
                required=False,
            ),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        """Execute the web fetch."""
        url = arguments.get("url", "")
        custom_headers = arguments.get("headers", {})
        timeout = arguments.get("timeout", self.timeout_seconds)

        # Validate URL
        if not url.startswith(("http://", "https://")):
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error="URL must start with http:// or https://",
            )

        # Prepare headers
        headers = {"User-Agent": self.user_agent}
        if custom_headers:
            headers.update(custom_headers)

        try:
            logger.info(f"Fetching URL: {url}")

            # Create timeout configuration
            timeout_config = aiohttp.ClientTimeout(total=timeout)

            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                async with session.get(url, headers=headers) as response:
                    # Check response size
                    content_length = response.headers.get("Content-Length")
                    if content_length and int(content_length) > self.max_response_size:
                        return ToolResult(
                            tool_name=self.name,
                            status=ToolStatus.ERROR,
                            error=f"Response too large: {content_length} bytes (max: {self.max_response_size})",
                            metadata={
                                "url": url,
                                "status_code": response.status,
                                "content_length": int(content_length),
                            },
                        )

                    # Read response with size limit
                    content_bytes = bytearray()
                    async for chunk in response.content.iter_chunked(8192):
                        content_bytes.extend(chunk)
                        if len(content_bytes) > self.max_response_size:
                            return ToolResult(
                                tool_name=self.name,
                                status=ToolStatus.ERROR,
                                error=f"Response exceeded max size of {self.max_response_size} bytes",
                                metadata={
                                    "url": url,
                                    "status_code": response.status,
                                },
                            )

                    # Try to decode as text
                    try:
                        content = content_bytes.decode("utf-8")
                    except UnicodeDecodeError:
                        # Try other common encodings
                        try:
                            content = content_bytes.decode("latin-1")
                        except:
                            content = f"<binary content, {len(content_bytes)} bytes>"

                    # Prepare response headers
                    response_headers = dict(response.headers)

                    # Determine status
                    status = ToolStatus.SUCCESS if 200 <= response.status < 300 else ToolStatus.ERROR

                    output = {
                        "content": content,
                        "status_code": response.status,
                        "headers": response_headers,
                        "url": str(response.url),  # Final URL after redirects
                    }

                    error = None
                    if status == ToolStatus.ERROR:
                        error = f"HTTP {response.status}: {response.reason}"

                    logger.info(
                        f"Web fetch completed: status={response.status}, "
                        f"size={len(content_bytes)} bytes"
                    )

                    return ToolResult(
                        tool_name=self.name,
                        status=status,
                        output=output,
                        error=error,
                        metadata={
                            "url": url,
                            "final_url": str(response.url),
                            "status_code": response.status,
                            "content_type": response.headers.get("Content-Type"),
                            "size": len(content_bytes),
                        },
                    )

        except asyncio.TimeoutError:
            logger.error(f"Web fetch timed out: {url}")
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Request timed out after {timeout} seconds",
                metadata={"url": url, "timeout": timeout},
            )

        except aiohttp.ClientError as e:
            logger.error(f"Web fetch failed: {str(e)}", exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Request failed: {str(e)}",
                metadata={"url": url},
            )

        except Exception as e:
            logger.error(f"Web fetch failed unexpectedly: {str(e)}", exc_info=True)
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=f"Unexpected error: {str(e)}",
                metadata={"url": url},
            )
