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

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse, urljoin

import aiohttp
from typing import Optional
from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
import logging

logger = logging.getLogger(__name__)


def _ip_is_blocked(ip_str: str) -> bool:
    """Return True if the IP belongs to a range we refuse to fetch from.

    Blocks loopback, link-local (incl. cloud metadata 169.254.169.254),
    private RFC1918, multicast, reserved, and unspecified ranges.
    """
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return (
        ip.is_loopback
        or ip.is_link_local
        or ip.is_private
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def _check_url_safe(url: str) -> Optional[str]:
    """Resolve hostname and reject if it points at a non-public address.

    Returns None on safe, or an error message on rejection. Does DNS lookup
    in a thread to avoid blocking the event loop.
    """
    try:
        parsed = urlparse(url)
    except Exception as exc:
        return f"Invalid URL: {exc}"
    host = parsed.hostname
    if not host:
        return "URL has no hostname"
    # Direct literal IP check first.
    try:
        ipaddress.ip_address(host)
        if _ip_is_blocked(host):
            return f"URL host '{host}' is in a blocked address range"
        return None
    except ValueError:
        pass  # not a literal IP, resolve via DNS
    try:
        infos = await asyncio.get_event_loop().getaddrinfo(
            host, None, type=socket.SOCK_STREAM
        )
    except socket.gaierror as exc:
        return f"DNS resolution failed for '{host}': {exc}"
    for info in infos:
        ip_str = info[4][0]
        if _ip_is_blocked(ip_str):
            return (
                f"URL host '{host}' resolves to blocked address {ip_str}"
            )
    return None


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
    def dependencies(self) -> list[str]:
        return ["http_client"]

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

        # SSRF guard on the initial URL
        ssrf_err = await _check_url_safe(url)
        if ssrf_err:
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.ERROR,
                error=ssrf_err,
                metadata={"url": url},
            )

        # Prepare headers
        headers = {"User-Agent": self.user_agent}
        if custom_headers:
            headers.update(custom_headers)

        try:
            logger.info(f"Fetching URL: {url}")

            # Create timeout configuration
            timeout_config = aiohttp.ClientTimeout(total=timeout)

            # Manual redirect handling so each hop re-validates against SSRF.
            # aiohttp's default allow_redirects=True does NOT re-resolve safety,
            # which would let an attacker-controlled public host 302 into a
            # private/loopback target on the tailnet.
            current_url = url
            max_redirects = 3
            async with aiohttp.ClientSession(timeout=timeout_config) as session:
                for hop in range(max_redirects + 1):
                    async with session.get(
                        current_url, headers=headers, allow_redirects=False
                    ) as response:
                        if response.status in (301, 302, 303, 307, 308):
                            if hop >= max_redirects:
                                return ToolResult(
                                    tool_name=self.name,
                                    status=ToolStatus.ERROR,
                                    error=f"Too many redirects (>{max_redirects})",
                                    metadata={"url": url},
                                )
                            location = response.headers.get("Location")
                            if not location:
                                # Treat malformed redirect as final; fall through
                                # to body processing below.
                                pass
                            else:
                                next_url = urljoin(current_url, location)
                                redirect_err = await _check_url_safe(next_url)
                                if redirect_err:
                                    return ToolResult(
                                        tool_name=self.name,
                                        status=ToolStatus.ERROR,
                                        error=f"Redirect blocked: {redirect_err}",
                                        metadata={
                                            "url": url,
                                            "redirect_to": next_url,
                                            "status_code": response.status,
                                        },
                                    )
                                current_url = next_url
                                continue

                        # Non-redirect (or malformed redirect) — process body.
                        content_length = response.headers.get("Content-Length")
                        try:
                            content_length_int = int(content_length) if content_length else 0
                        except (ValueError, TypeError):
                            content_length_int = 0
                        if content_length_int > self.max_response_size:
                            return ToolResult(
                                tool_name=self.name,
                                status=ToolStatus.ERROR,
                                error=f"Response too large: {content_length_int} bytes (max: {self.max_response_size})",
                                metadata={
                                    "url": url,
                                    "status_code": response.status,
                                    "content_length": content_length_int,
                                },
                            )

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

                        try:
                            content = content_bytes.decode("utf-8")
                        except UnicodeDecodeError:
                            try:
                                content = content_bytes.decode("latin-1")
                            except Exception:
                                content = f"<binary content, {len(content_bytes)} bytes>"

                        response_headers = dict(response.headers)
                        status = ToolStatus.SUCCESS if 200 <= response.status < 300 else ToolStatus.ERROR

                        output = {
                            "content": content,
                            "status_code": response.status,
                            "headers": response_headers,
                            "url": str(response.url),
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
