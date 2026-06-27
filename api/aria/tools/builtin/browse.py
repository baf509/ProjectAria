"""
ARIA - Browse Page Tool

Purpose: Fetch a web page and return its readable text (title + main content),
following redirects — a lightweight step up from web_fetch. For full computer-use
(JS rendering, clicking, screenshots), add the Playwright MCP server via
`aria mcp add playwright "npx @playwright/mcp@latest"`.
"""

from __future__ import annotations

import html
import re

import httpx

from ..base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType

_SCRIPT_STYLE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")
_SPACES = re.compile(r"[ \t]+")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def _readable(raw: str) -> tuple[str, str]:
    title_m = _TITLE.search(raw)
    title = html.unescape(title_m.group(1).strip()) if title_m else ""
    body = _SCRIPT_STYLE.sub(" ", raw)
    body = _TAG.sub(" ", body)
    body = html.unescape(body)
    body = _SPACES.sub(" ", body)
    lines = [ln.strip() for ln in body.splitlines()]
    body = "\n".join(ln for ln in lines if ln)
    return title, body.strip()


class BrowsePageTool(BaseTool):
    @property
    def name(self) -> str:
        return "browse_page"

    @property
    def type(self) -> ToolType:
        return ToolType.BUILTIN

    @property
    def description(self) -> str:
        return (
            "Fetch a web page and return its readable text (title + main content), "
            "following redirects. Use to read articles, docs, or pages."
        )

    @property
    def parameters(self) -> list[ToolParameter]:
        return [
            ToolParameter(name="url", type="string", description="The URL to fetch", required=True),
            ToolParameter(name="max_chars", type="number", description="Max characters of text to return", required=False, default=4000),
        ]

    async def execute(self, arguments: dict) -> ToolResult:
        url = (arguments.get("url") or "").strip()
        if not url:
            return ToolResult(tool_name=self.name, status=ToolStatus.ERROR, error="url is required")
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        max_chars = int(arguments.get("max_chars", 4000) or 4000)
        try:
            async with httpx.AsyncClient(
                timeout=15.0,
                follow_redirects=True,
                headers={"User-Agent": "ARIA/0.2 (+browse_page)"},
            ) as client:
                resp = await client.get(url)
            ctype = resp.headers.get("content-type", "")
            raw = resp.text[:500_000]
            if "html" in ctype or raw.lstrip()[:1] == "<":
                title, body = _readable(raw)
            else:
                title, body = "", raw
            truncated = len(body) > max_chars
            return ToolResult(
                tool_name=self.name,
                status=ToolStatus.SUCCESS,
                output={
                    "url": str(resp.url),
                    "status_code": resp.status_code,
                    "title": title,
                    "content": body[:max_chars],
                    "truncated": truncated,
                },
            )
        except Exception as e:
            return ToolResult(tool_name=self.name, status=ToolStatus.ERROR, error=f"browse failed: {e}")
