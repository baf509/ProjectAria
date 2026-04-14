"""Tests for every built-in tool — real logic, mocked I/O.

Each tool section verifies:
  1. Properties (name, description, type, parameters, dependencies)
  2. execute() success path
  3. execute() error / edge-case paths
  4. Argument validation integration (required params, enums, etc.)
"""

import asyncio
import base64
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest

from aria.tools.base import ToolResult, ToolStatus, ToolType


# ============================================================================
# WebTool
# ============================================================================

class TestWebTool:
    """Tests for aria.tools.builtin.web.WebTool."""

    def _make_tool(self, **kwargs):
        from aria.tools.builtin.web import WebTool
        return WebTool(**kwargs)

    # -- Properties --

    def test_properties(self):
        tool = self._make_tool()
        assert tool.name == "web_fetch"
        assert tool.type == ToolType.BUILTIN
        assert tool.dependencies == ["http_client"]
        param_names = {p.name for p in tool.parameters}
        assert param_names == {"url", "headers", "timeout"}

    # -- Validation --

    @pytest.mark.asyncio
    async def test_rejects_non_http_url(self):
        tool = self._make_tool()
        result = await tool.execute({"url": "ftp://example.com"})
        assert result.status == ToolStatus.ERROR
        assert "http://" in result.error

    # -- Success path (mock aiohttp) --

    @pytest.mark.asyncio
    async def test_fetch_success(self):
        tool = self._make_tool()

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Type": "text/html", "Content-Length": "13"}
        mock_response.url = "https://example.com"
        mock_response.content.iter_chunked = lambda _: _async_iter([b"Hello, World!"])

        mock_session = MagicMock()
        # session.get(url, headers=...) must return an async context manager
        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session.get = MagicMock(return_value=mock_ctx)

        # ClientSession() must also be an async context manager
        mock_cs = MagicMock()
        mock_cs.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cs.__aexit__ = AsyncMock(return_value=False)

        with patch("aria.tools.builtin.web.aiohttp.ClientSession", return_value=mock_cs):
            result = await tool.execute({"url": "https://example.com"})

        assert result.status == ToolStatus.SUCCESS
        assert result.output["content"] == "Hello, World!"
        assert result.output["status_code"] == 200

    # -- HTTP error status --

    @pytest.mark.asyncio
    async def test_fetch_http_error_status(self):
        tool = self._make_tool()

        mock_response = AsyncMock()
        mock_response.status = 404
        mock_response.reason = "Not Found"
        mock_response.headers = {"Content-Type": "text/html"}
        mock_response.url = "https://example.com/missing"
        mock_response.content.iter_chunked = lambda _: _async_iter([b"not found"])

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_ctx)

        mock_cs = MagicMock()
        mock_cs.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cs.__aexit__ = AsyncMock(return_value=False)

        with patch("aria.tools.builtin.web.aiohttp.ClientSession", return_value=mock_cs):
            result = await tool.execute({"url": "https://example.com/missing"})

        assert result.status == ToolStatus.ERROR
        assert "404" in result.error

    # -- Timeout --

    @pytest.mark.asyncio
    async def test_fetch_timeout(self):
        tool = self._make_tool()

        import aiohttp
        with patch("aria.tools.builtin.web.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(
                side_effect=asyncio.TimeoutError()
            )
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await tool.execute({"url": "https://slow.example.com"})

        assert result.status == ToolStatus.ERROR
        assert "timed out" in result.error

    # -- Response too large (Content-Length header) --

    @pytest.mark.asyncio
    async def test_fetch_response_too_large_header(self):
        tool = self._make_tool(max_response_size=100)

        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.reason = "OK"
        mock_response.headers = {"Content-Length": "999999"}
        mock_response.url = "https://example.com/big"

        mock_ctx = MagicMock()
        mock_ctx.__aenter__ = AsyncMock(return_value=mock_response)
        mock_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock()
        mock_session.get = MagicMock(return_value=mock_ctx)

        mock_cs = MagicMock()
        mock_cs.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cs.__aexit__ = AsyncMock(return_value=False)

        with patch("aria.tools.builtin.web.aiohttp.ClientSession", return_value=mock_cs):
            result = await tool.execute({"url": "https://example.com/big"})

        assert result.status == ToolStatus.ERROR
        assert "too large" in result.error

    # -- Connection error --

    @pytest.mark.asyncio
    async def test_fetch_client_error(self):
        import aiohttp
        tool = self._make_tool()

        with patch("aria.tools.builtin.web.aiohttp.ClientSession") as MockSession:
            MockSession.return_value.__aenter__ = AsyncMock(
                side_effect=aiohttp.ClientError("Connection refused")
            )
            MockSession.return_value.__aexit__ = AsyncMock(return_value=False)
            result = await tool.execute({"url": "https://down.example.com"})

        assert result.status == ToolStatus.ERROR
        assert "Request failed" in result.error

    # -- Custom headers forwarded --

    @pytest.mark.asyncio
    async def test_custom_headers(self):
        tool = self._make_tool()

        captured_headers = {}
        mock_response = AsyncMock()
        mock_response.status = 200
        mock_response.reason = "OK"
        mock_response.headers = {}
        mock_response.url = "https://api.example.com"
        mock_response.content.iter_chunked = lambda _: _async_iter([b"ok"])

        def mock_get(url, headers=None):
            captured_headers.update(headers or {})
            ctx = MagicMock()
            ctx.__aenter__ = AsyncMock(return_value=mock_response)
            ctx.__aexit__ = AsyncMock(return_value=False)
            return ctx

        mock_session = MagicMock()
        mock_session.get = mock_get

        mock_cs = MagicMock()
        mock_cs.__aenter__ = AsyncMock(return_value=mock_session)
        mock_cs.__aexit__ = AsyncMock(return_value=False)

        with patch("aria.tools.builtin.web.aiohttp.ClientSession", return_value=mock_cs):
            await tool.execute({
                "url": "https://api.example.com",
                "headers": {"Authorization": "Bearer tok123"},
            })

        assert captured_headers.get("Authorization") == "Bearer tok123"
        assert "User-Agent" in captured_headers


# ============================================================================
# ShellTool
# ============================================================================

class TestShellTool:
    """Tests for aria.tools.builtin.shell.ShellTool."""

    def _make_tool(self, **kwargs):
        from aria.tools.builtin.shell import ShellTool
        return ShellTool(**kwargs)

    # -- Properties --

    def test_properties(self):
        tool = self._make_tool()
        assert tool.name == "shell"
        assert tool.type == ToolType.BUILTIN
        assert tool.dependencies == ["shell"]
        param_names = {p.name for p in tool.parameters}
        assert param_names == {"command", "working_directory", "timeout"}

    # -- Command validation --

    def test_validate_rejects_pipes(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("cat /etc/passwd | grep root")
        assert ok is False
        assert "disallowed" in err

    def test_validate_rejects_semicolon(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("echo hi; rm -rf /")
        assert ok is False

    def test_validate_rejects_backtick(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("echo `whoami`")
        assert ok is False

    def test_validate_rejects_subshell(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("echo $(whoami)")
        assert ok is False

    def test_validate_rejects_redirect(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("echo hi > /tmp/x")
        assert ok is False

    def test_validate_accepts_simple_command(self):
        tool = self._make_tool()
        ok, err = tool._validate_command("ls -la /tmp")
        assert ok is True
        assert err is None

    def test_validate_denied_commands(self):
        tool = self._make_tool(denied_commands=["rm", "dd"])
        ok, err = tool._validate_command("rm -rf /")
        assert ok is False
        assert "denied" in err

    def test_validate_allowlist(self):
        tool = self._make_tool(allowed_commands=["ls", "cat"])
        ok, err = tool._validate_command("whoami")
        assert ok is False
        assert "not in allowed" in err

    def test_validate_allowlist_permits(self):
        tool = self._make_tool(allowed_commands=["ls", "cat"])
        ok, err = tool._validate_command("ls /tmp")
        assert ok is True

    # -- Execute success --

    @pytest.mark.asyncio
    async def test_execute_echo(self):
        """Actually run a real simple command (echo is safe)."""
        tool = self._make_tool()
        result = await tool.execute({"command": "echo hello-aria"})
        assert result.status == ToolStatus.SUCCESS
        assert result.output["stdout"].strip() == "hello-aria"
        assert result.output["exit_code"] == 0

    # -- Execute failure (non-zero exit) --

    @pytest.mark.asyncio
    async def test_execute_nonzero_exit(self):
        tool = self._make_tool()
        result = await tool.execute({"command": "false"})
        assert result.status == ToolStatus.ERROR
        assert result.output["exit_code"] != 0

    # -- Execute timeout --

    @pytest.mark.asyncio
    async def test_execute_timeout(self):
        tool = self._make_tool(timeout_seconds=1)
        result = await tool.execute({"command": "sleep 60", "timeout": 0.1})
        assert result.status == ToolStatus.ERROR
        assert "timed out" in result.error

    # -- Command injection blocked --

    @pytest.mark.asyncio
    async def test_injection_blocked_at_execute(self):
        tool = self._make_tool()
        result = await tool.execute({"command": "echo hi && rm -rf /"})
        assert result.status == ToolStatus.ERROR
        assert "disallowed" in result.error

    # -- Working directory --

    @pytest.mark.asyncio
    async def test_working_directory(self, tmp_path):
        tool = self._make_tool()
        result = await tool.execute({
            "command": "pwd",
            "working_directory": str(tmp_path),
        })
        assert result.status == ToolStatus.SUCCESS
        assert str(tmp_path) in result.output["stdout"]


# ============================================================================
# FilesystemTool
# ============================================================================

class TestFilesystemTool:
    """Tests for aria.tools.builtin.filesystem.FilesystemTool."""

    def _make_tool(self, tmp_path):
        from aria.tools.builtin.filesystem import FilesystemTool
        return FilesystemTool(allowed_paths=[str(tmp_path)])

    # -- Properties --

    def test_properties(self, tmp_path):
        tool = self._make_tool(tmp_path)
        assert tool.name == "filesystem"
        assert tool.type == ToolType.BUILTIN
        param_names = {p.name for p in tool.parameters}
        assert param_names == {"operation", "path", "content", "create_parents"}

    # -- read_file --

    @pytest.mark.asyncio
    async def test_read_file(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "hello.txt"
        f.write_text("Hello ARIA!")
        result = await tool.execute({"operation": "read_file", "path": str(f)})
        assert result.status == ToolStatus.SUCCESS
        assert result.output == "Hello ARIA!"

    @pytest.mark.asyncio
    async def test_read_file_not_found(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "read_file", "path": str(tmp_path / "nope.txt")})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_read_binary_file(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "data.bin"
        f.write_bytes(b"\x00\xff\xfe")
        result = await tool.execute({"operation": "read_file", "path": str(f)})
        assert result.status == ToolStatus.SUCCESS
        assert "binary" in result.output.lower()

    # -- write_file --

    @pytest.mark.asyncio
    async def test_write_file(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "out.txt"
        result = await tool.execute({
            "operation": "write_file",
            "path": str(f),
            "content": "written by test",
        })
        assert result.status == ToolStatus.SUCCESS
        assert f.read_text() == "written by test"

    @pytest.mark.asyncio
    async def test_write_file_create_parents(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "deep" / "nested" / "file.txt"
        result = await tool.execute({
            "operation": "write_file",
            "path": str(f),
            "content": "deep",
            "create_parents": True,
        })
        assert result.status == ToolStatus.SUCCESS
        assert f.read_text() == "deep"

    @pytest.mark.asyncio
    async def test_write_file_no_parent(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "no_parent" / "file.txt"
        result = await tool.execute({
            "operation": "write_file",
            "path": str(f),
            "content": "fail",
        })
        assert result.status == ToolStatus.ERROR
        assert "parent" in result.error.lower()

    # -- list_directory --

    @pytest.mark.asyncio
    async def test_list_directory(self, tmp_path):
        tool = self._make_tool(tmp_path)
        (tmp_path / "a.txt").write_text("a")
        (tmp_path / "subdir").mkdir()
        result = await tool.execute({"operation": "list_directory", "path": str(tmp_path)})
        assert result.status == ToolStatus.SUCCESS
        names = {e["name"] for e in result.output}
        assert "a.txt" in names
        assert "subdir" in names

    @pytest.mark.asyncio
    async def test_list_directory_not_found(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "list_directory", "path": str(tmp_path / "nope")})
        assert result.status == ToolStatus.ERROR

    # -- create_directory --

    @pytest.mark.asyncio
    async def test_create_directory(self, tmp_path):
        tool = self._make_tool(tmp_path)
        d = tmp_path / "newdir"
        result = await tool.execute({"operation": "create_directory", "path": str(d)})
        assert result.status == ToolStatus.SUCCESS
        assert d.is_dir()

    @pytest.mark.asyncio
    async def test_create_directory_already_exists(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "create_directory", "path": str(tmp_path)})
        assert result.status == ToolStatus.ERROR

    # -- delete_file --

    @pytest.mark.asyncio
    async def test_delete_file(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "delete_me.txt"
        f.write_text("bye")
        result = await tool.execute({"operation": "delete_file", "path": str(f)})
        assert result.status == ToolStatus.SUCCESS
        assert not f.exists()

    @pytest.mark.asyncio
    async def test_delete_file_rejects_directory(self, tmp_path):
        tool = self._make_tool(tmp_path)
        d = tmp_path / "adir"
        d.mkdir()
        result = await tool.execute({"operation": "delete_file", "path": str(d)})
        assert result.status == ToolStatus.ERROR
        assert "directory" in result.error.lower()

    # -- file_exists --

    @pytest.mark.asyncio
    async def test_file_exists_true(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "exists.txt"
        f.write_text("yes")
        result = await tool.execute({"operation": "file_exists", "path": str(f)})
        assert result.status == ToolStatus.SUCCESS
        assert result.output is True

    @pytest.mark.asyncio
    async def test_file_exists_false(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "file_exists", "path": str(tmp_path / "nope")})
        assert result.status == ToolStatus.SUCCESS
        assert result.output is False

    # -- get_file_info --

    @pytest.mark.asyncio
    async def test_get_file_info(self, tmp_path):
        tool = self._make_tool(tmp_path)
        f = tmp_path / "info.txt"
        f.write_text("data")
        result = await tool.execute({"operation": "get_file_info", "path": str(f)})
        assert result.status == ToolStatus.SUCCESS
        assert result.output["name"] == "info.txt"
        assert result.output["type"] == "file"
        assert result.output["size"] == 4

    # -- Path validation (security) --

    @pytest.mark.asyncio
    async def test_rejects_path_outside_allowed(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "read_file", "path": "/etc/passwd"})
        assert result.status == ToolStatus.ERROR
        assert "denied" in result.error.lower() or "outside" in result.error.lower()

    @pytest.mark.asyncio
    async def test_rejects_denied_path(self, tmp_path):
        from aria.tools.builtin.filesystem import FilesystemTool
        denied = tmp_path / "secret"
        denied.mkdir()
        tool = FilesystemTool(allowed_paths=[str(tmp_path)], denied_paths=[str(denied)])
        result = await tool.execute({"operation": "read_file", "path": str(denied / "key.pem")})
        assert result.status == ToolStatus.ERROR
        assert "denied" in result.error.lower()

    # -- Unknown operation --

    @pytest.mark.asyncio
    async def test_unknown_operation(self, tmp_path):
        tool = self._make_tool(tmp_path)
        result = await tool.execute({"operation": "rename_file", "path": str(tmp_path)})
        assert result.status == ToolStatus.ERROR
        assert "unknown" in result.error.lower()


# ============================================================================
# ScreenshotTool
# ============================================================================

class TestScreenshotTool:
    """Tests for aria.tools.builtin.screenshot.ScreenshotTool."""

    def _make_tool(self):
        with patch("aria.tools.builtin.screenshot.settings"), \
             patch("aria.tools.builtin.screenshot.llm_manager"):
            from aria.tools.builtin.screenshot import ScreenshotTool
            return ScreenshotTool()

    # -- Properties --

    def test_properties(self):
        tool = self._make_tool()
        assert tool.name == "screenshot_analyze"
        assert tool.type == ToolType.BUILTIN

    # -- No display --

    @pytest.mark.asyncio
    async def test_no_display_error(self):
        tool = self._make_tool()
        with patch.dict(os.environ, {}, clear=True):
            # Ensure DISPLAY and WAYLAND_DISPLAY are not set
            os.environ.pop("DISPLAY", None)
            os.environ.pop("WAYLAND_DISPLAY", None)
            result = await tool.execute({"query": "What's on screen?"})
        assert result.status == ToolStatus.ERROR
        assert "No display" in result.error

    # -- Screenshot capture + vision analysis success --

    @pytest.mark.asyncio
    async def test_screenshot_success(self, tmp_path):
        tool = self._make_tool()

        fake_png = b"\x89PNG fake image data"
        tmp_file = str(tmp_path / "shot.png")

        async def fake_subprocess(*args, **kwargs):
            # Write fake PNG to the path argument
            with open(args[1], "wb") as f:
                f.write(fake_png)
            proc = AsyncMock()
            proc.returncode = 0
            proc.communicate = AsyncMock(return_value=(b"", b""))
            return proc

        fake_adapter = AsyncMock()

        async def fake_stream(*a, **kw):
            from aria.llm.base import StreamChunk
            yield StreamChunk(type="text", content="A desktop with terminal open")
            yield StreamChunk(type="done", usage={})

        fake_adapter.stream = fake_stream

        with patch.dict(os.environ, {"DISPLAY": ":0"}), \
             patch("aria.tools.builtin.screenshot.settings") as mock_settings, \
             patch("aria.tools.builtin.screenshot.llm_manager") as mock_llm_mgr, \
             patch("aria.tools.builtin.screenshot.asyncio.create_subprocess_exec", side_effect=fake_subprocess), \
             patch("aria.tools.builtin.screenshot.tempfile.NamedTemporaryFile") as mock_tmp:

            mock_settings.screenshot_command = "grim"
            mock_settings.screenshot_vision_backend = "anthropic"
            mock_settings.screenshot_vision_model = "claude-sonnet"
            mock_llm_mgr.get_adapter.return_value = fake_adapter

            # Make the temp file point to our controlled path
            mock_tmp.return_value.__enter__ = MagicMock(return_value=MagicMock(name=tmp_file))
            mock_tmp.return_value.__exit__ = MagicMock(return_value=False)

            # Patch NamedTemporaryFile to write to tmp_path
            with patch("aria.tools.builtin.screenshot.tempfile.NamedTemporaryFile") as mock_ntf:
                ctx = MagicMock()
                ctx.name = tmp_file
                mock_ntf.return_value.__enter__ = MagicMock(return_value=ctx)
                mock_ntf.return_value.__exit__ = MagicMock(return_value=False)

                result = await tool.execute({"query": "What's on screen?"})

        assert result.status == ToolStatus.SUCCESS
        assert "desktop" in result.output.lower() or "terminal" in result.output.lower()

    # -- Screenshot command not found --

    @pytest.mark.asyncio
    async def test_screenshot_command_not_found(self):
        tool = self._make_tool()

        with patch.dict(os.environ, {"DISPLAY": ":0"}), \
             patch("aria.tools.builtin.screenshot.settings") as mock_settings, \
             patch("aria.tools.builtin.screenshot.asyncio.create_subprocess_exec",
                   side_effect=FileNotFoundError("grim not found")), \
             patch("aria.tools.builtin.screenshot.tempfile.NamedTemporaryFile") as mock_ntf:

            mock_settings.screenshot_command = "grim"
            ctx = MagicMock()
            ctx.name = "/tmp/fake.png"
            mock_ntf.return_value.__enter__ = MagicMock(return_value=ctx)
            mock_ntf.return_value.__exit__ = MagicMock(return_value=False)

            result = await tool.execute({})

        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error.lower()


# ============================================================================
# Coding Session Tools (6 tools)
# ============================================================================

class TestCodingSessionTools:
    """Tests for all 6 coding session tools."""

    def _make_manager(self):
        mgr = AsyncMock()
        mgr.start_session = AsyncMock(return_value={
            "session_id": "sess-1",
            "status": "running",
            "workspace": "/tmp/ws",
        })
        mgr.stop_session = AsyncMock(return_value=True)
        mgr.get_output = AsyncMock(return_value="line1\nline2\n")
        mgr.send_input = AsyncMock(return_value=True)
        mgr.list_sessions = AsyncMock(return_value=[
            {"session_id": "sess-1", "status": "running"},
        ])
        mgr.get_diff = AsyncMock(return_value="diff --git a/f.py b/f.py\n+new line")
        return mgr

    # -- StartCodingSessionTool --

    @pytest.mark.asyncio
    async def test_start_session(self):
        from aria.tools.builtin.coding import StartCodingSessionTool
        mgr = self._make_manager()
        tool = StartCodingSessionTool(mgr)
        assert tool.name == "start_coding_session"
        assert tool.type == ToolType.BUILTIN

        result = await tool.execute({"workspace": "/tmp/ws", "prompt": "Fix the bug"})
        assert result.status == ToolStatus.SUCCESS
        assert result.output["session_id"] == "sess-1"
        mgr.start_session.assert_called_once_with(workspace="/tmp/ws", prompt="Fix the bug")

    # -- StopCodingSessionTool --

    @pytest.mark.asyncio
    async def test_stop_session(self):
        from aria.tools.builtin.coding import StopCodingSessionTool
        mgr = self._make_manager()
        tool = StopCodingSessionTool(mgr)
        assert tool.name == "stop_coding_session"

        result = await tool.execute({"session_id": "sess-1"})
        assert result.status == ToolStatus.SUCCESS
        assert result.output["stopped"] is True

    # -- GetCodingOutputTool --

    @pytest.mark.asyncio
    async def test_get_output(self):
        from aria.tools.builtin.coding import GetCodingOutputTool
        mgr = self._make_manager()
        tool = GetCodingOutputTool(mgr)
        assert tool.name == "get_coding_output"

        result = await tool.execute({"session_id": "sess-1", "lines": 100})
        assert result.status == ToolStatus.SUCCESS
        assert "line1" in result.output["output"]
        mgr.get_output.assert_called_once_with("sess-1", lines=100)

    @pytest.mark.asyncio
    async def test_get_output_default_lines(self):
        from aria.tools.builtin.coding import GetCodingOutputTool
        mgr = self._make_manager()
        tool = GetCodingOutputTool(mgr)
        result = await tool.execute({"session_id": "sess-1"})
        assert result.status == ToolStatus.SUCCESS
        mgr.get_output.assert_called_once_with("sess-1", lines=50)

    # -- SendToCodingSessionTool --

    @pytest.mark.asyncio
    async def test_send_input(self):
        from aria.tools.builtin.coding import SendToCodingSessionTool
        mgr = self._make_manager()
        tool = SendToCodingSessionTool(mgr)
        assert tool.name == "send_to_coding_session"

        result = await tool.execute({"session_id": "sess-1", "text": "y\n"})
        assert result.status == ToolStatus.SUCCESS
        assert result.output["sent"] is True

    # -- ListCodingSessionsTool --

    @pytest.mark.asyncio
    async def test_list_sessions(self):
        from aria.tools.builtin.coding import ListCodingSessionsTool
        mgr = self._make_manager()
        tool = ListCodingSessionsTool(mgr)
        assert tool.name == "list_coding_sessions"

        result = await tool.execute({})
        assert result.status == ToolStatus.SUCCESS
        assert len(result.output["sessions"]) == 1

    @pytest.mark.asyncio
    async def test_list_sessions_with_filter(self):
        from aria.tools.builtin.coding import ListCodingSessionsTool
        mgr = self._make_manager()
        tool = ListCodingSessionsTool(mgr)
        await tool.execute({"status": "running"})
        mgr.list_sessions.assert_called_once_with(status="running")

    # -- GetCodingDiffTool --

    @pytest.mark.asyncio
    async def test_get_diff(self):
        from aria.tools.builtin.coding import GetCodingDiffTool
        mgr = self._make_manager()
        tool = GetCodingDiffTool(mgr)
        assert tool.name == "get_coding_diff"

        result = await tool.execute({"session_id": "sess-1"})
        assert result.status == ToolStatus.SUCCESS
        assert "diff" in result.output["diff"]


# ============================================================================
# ClaudeAgentTool
# ============================================================================

class TestClaudeAgentTool:
    """Tests for aria.tools.builtin.claude_agent.ClaudeAgentTool."""

    @pytest.mark.asyncio
    async def test_properties(self):
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s:
            mock_s.claude_runner_timeout_seconds = 120
            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()
        assert tool.name == "claude_agent"
        assert tool.type == ToolType.BUILTIN
        assert tool.dependencies == ["claude_cli"]

    @pytest.mark.asyncio
    async def test_empty_task_error(self):
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s:
            mock_s.claude_runner_timeout_seconds = 120
            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()
        result = await tool.execute({"task": ""})
        assert result.status == ToolStatus.ERROR
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_cli_not_available(self):
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s, \
             patch("aria.tools.builtin.claude_agent.ClaudeRunner") as MockRunner:
            mock_s.claude_runner_timeout_seconds = 120
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = False
            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()
            result = await tool.execute({"task": "do something"})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_execute_success(self):
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s, \
             patch("aria.tools.builtin.claude_agent.ClaudeRunner") as MockRunner:
            mock_s.claude_runner_timeout_seconds = 120
            mock_s.coding_default_workspace = "/tmp"
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = True

            runner_instance = AsyncMock()
            runner_instance.run = AsyncMock(return_value="Task completed successfully")
            MockRunner.return_value = runner_instance

            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()
            result = await tool.execute({"task": "analyze this code"})

        assert result.status == ToolStatus.SUCCESS
        assert result.output["response"] == "Task completed successfully"

    @pytest.mark.asyncio
    async def test_execute_failure(self):
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s, \
             patch("aria.tools.builtin.claude_agent.ClaudeRunner") as MockRunner:
            mock_s.claude_runner_timeout_seconds = 120
            mock_s.coding_default_workspace = "/tmp"
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = True

            runner_instance = AsyncMock()
            runner_instance.run = AsyncMock(return_value=None)
            runner_instance.last_error = "timed out"
            MockRunner.return_value = runner_instance

            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()
            result = await tool.execute({"task": "long task"})

        assert result.status == ToolStatus.ERROR
        assert "timed out" in result.error

    @pytest.mark.asyncio
    async def test_estop_blocks_execution(self):
        """Emergency stop should prevent agent spawning."""
        with patch("aria.tools.builtin.claude_agent.settings") as mock_s:
            mock_s.claude_runner_timeout_seconds = 120
            from aria.tools.builtin.claude_agent import ClaudeAgentTool
            tool = ClaudeAgentTool()

        mock_estop = AsyncMock()
        mock_estop.is_active.return_value = True
        mock_estop.get_state.return_value = MagicMock(reason="safety concern")

        mock_db = AsyncMock()

        with patch("aria.tools.builtin.claude_agent.ClaudeRunner"), \
             patch.dict("sys.modules", {}), \
             patch("aria.api.deps.get_db", return_value=mock_db), \
             patch("aria.api.deps.get_estop_manager", return_value=mock_estop):
            result = await tool.execute({"task": "do something dangerous"})

        assert result.status == ToolStatus.ERROR
        assert "emergency stop" in result.error.lower() or "Emergency stop" in result.error


# ============================================================================
# DeepThinkTool
# ============================================================================

class TestDeepThinkTool:
    """Tests for aria.tools.builtin.deep_think.DeepThinkTool."""

    @pytest.mark.asyncio
    async def test_properties(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s:
            mock_s.deep_think_timeout_seconds = 180
            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
        assert tool.name == "deep_think"
        assert tool.type == ToolType.BUILTIN
        assert tool.dependencies == ["claude_cli"]

    @pytest.mark.asyncio
    async def test_empty_prompt_error(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s:
            mock_s.deep_think_timeout_seconds = 180
            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
        result = await tool.execute({"prompt": ""})
        assert result.status == ToolStatus.ERROR

    @pytest.mark.asyncio
    async def test_cli_not_available(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s, \
             patch("aria.tools.builtin.deep_think.ClaudeRunner") as MockRunner:
            mock_s.deep_think_timeout_seconds = 180
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = False
            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
            result = await tool.execute({"prompt": "think hard"})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s, \
             patch("aria.tools.builtin.deep_think.ClaudeRunner") as MockRunner:
            mock_s.deep_think_timeout_seconds = 180
            mock_s.deep_think_model = "opus"
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = True

            runner = AsyncMock()
            runner.run = AsyncMock(return_value="The answer is 42.")
            MockRunner.return_value = runner

            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
            result = await tool.execute({"prompt": "What is the meaning of life?"})

        assert result.status == ToolStatus.SUCCESS
        assert "42" in result.output

    @pytest.mark.asyncio
    async def test_context_prepended(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s, \
             patch("aria.tools.builtin.deep_think.ClaudeRunner") as MockRunner:
            mock_s.deep_think_timeout_seconds = 180
            mock_s.deep_think_model = ""
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = True

            runner = AsyncMock()
            runner.run = AsyncMock(return_value="response")
            MockRunner.return_value = runner

            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
            await tool.execute({
                "prompt": "Summarize this",
                "context": "ARIA is an AI assistant",
            })

        # Verify context was prepended to the prompt
        call_args = runner.run.call_args[0][0]
        assert "ARIA is an AI assistant" in call_args
        assert "Summarize this" in call_args
        assert call_args.index("ARIA") < call_args.index("Summarize")

    @pytest.mark.asyncio
    async def test_failure_returns_none(self):
        with patch("aria.tools.builtin.deep_think.settings") as mock_s, \
             patch("aria.tools.builtin.deep_think.ClaudeRunner") as MockRunner:
            mock_s.deep_think_timeout_seconds = 180
            mock_s.deep_think_model = ""
            mock_s.claude_code_binary = "claude"
            MockRunner.is_available.return_value = True

            runner = AsyncMock()
            runner.run = AsyncMock(return_value=None)
            MockRunner.return_value = runner

            from aria.tools.builtin.deep_think import DeepThinkTool
            tool = DeepThinkTool()
            result = await tool.execute({"prompt": "think"})

        assert result.status == ToolStatus.ERROR
        assert "no output" in result.error.lower()


# ============================================================================
# Model Switch Tools
# ============================================================================

class TestModelSwitchTools:
    """Tests for ListLlamaCppModelsTool and SwitchLlamaCppModelTool."""

    @pytest.mark.asyncio
    async def test_list_models(self):
        mock_model = MagicMock()
        mock_model.to_dict.return_value = {"name": "qwen3", "active": True, "size_gb": 8.5}

        mock_switcher = AsyncMock()
        mock_switcher.list_models = AsyncMock(return_value=[mock_model])

        with patch("aria.tools.builtin.model_switch.LlamaCppModelSwitcher", return_value=mock_switcher):
            from aria.tools.builtin.model_switch import ListLlamaCppModelsTool
            tool = ListLlamaCppModelsTool()

        assert tool.name == "list_llamacpp_models"
        assert tool.parameters == []

        result = await tool.execute({})
        assert result.status == ToolStatus.SUCCESS
        assert len(result.output["models"]) == 1
        assert result.output["models"][0]["name"] == "qwen3"

    @pytest.mark.asyncio
    async def test_switch_model_success(self):
        mock_switcher = AsyncMock()
        mock_switcher.switch_model = AsyncMock(return_value={"switched": True, "model": "mistral"})

        with patch("aria.tools.builtin.model_switch.LlamaCppModelSwitcher", return_value=mock_switcher):
            from aria.tools.builtin.model_switch import SwitchLlamaCppModelTool
            tool = SwitchLlamaCppModelTool()

        assert tool.name == "switch_llamacpp_model"
        result = await tool.execute({"model_name": "mistral", "restart": True})
        assert result.status == ToolStatus.SUCCESS
        mock_switcher.switch_model.assert_called_once_with(model_name="mistral", restart=True)

    @pytest.mark.asyncio
    async def test_switch_model_error(self):
        mock_switcher = AsyncMock()
        mock_switcher.switch_model = AsyncMock(side_effect=RuntimeError("model not found"))

        with patch("aria.tools.builtin.model_switch.LlamaCppModelSwitcher", return_value=mock_switcher):
            from aria.tools.builtin.model_switch import SwitchLlamaCppModelTool
            tool = SwitchLlamaCppModelTool()

        result = await tool.execute({"model_name": "nonexistent"})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error


# ============================================================================
# DocumentGenerationTool
# ============================================================================

class TestDocumentGenerationTool:
    """Tests for aria.tools.builtin.docgen.DocumentGenerationTool."""

    @pytest.mark.asyncio
    async def test_properties(self):
        with patch("aria.tools.builtin.docgen.settings"):
            from aria.tools.builtin.docgen import DocumentGenerationTool
            tool = DocumentGenerationTool()
        assert tool.name == "generate_document"
        assert tool.type == ToolType.BUILTIN
        param_names = {p.name for p in tool.parameters}
        assert param_names == {"format", "filename", "content"}

    @pytest.mark.asyncio
    async def test_unsupported_format(self):
        with patch("aria.tools.builtin.docgen.settings"):
            from aria.tools.builtin.docgen import DocumentGenerationTool
            tool = DocumentGenerationTool()
        result = await tool.execute({"format": "txt", "filename": "test", "content": {}})
        assert result.status == ToolStatus.ERROR
        assert "unsupported" in result.error.lower()

    @pytest.mark.asyncio
    async def test_generate_docx(self, tmp_path):
        with patch("aria.tools.builtin.docgen.settings") as mock_s:
            mock_s.docgen_output_dir = str(tmp_path)
            from aria.tools.builtin.docgen import DocumentGenerationTool
            tool = DocumentGenerationTool()

        result = await tool.execute({
            "format": "docx",
            "filename": "test_doc",
            "content": {
                "title": "Test Document",
                "paragraphs": ["Paragraph one.", "Paragraph two."],
            },
        })

        # python-docx may or may not be installed
        if result.status == ToolStatus.SUCCESS:
            assert (tmp_path / "test_doc.docx").exists()
            assert "saved" in result.output.lower()
        else:
            assert "python-docx" in result.error

    @pytest.mark.asyncio
    async def test_generate_xlsx(self, tmp_path):
        with patch("aria.tools.builtin.docgen.settings") as mock_s:
            mock_s.docgen_output_dir = str(tmp_path)
            from aria.tools.builtin.docgen import DocumentGenerationTool
            tool = DocumentGenerationTool()

        result = await tool.execute({
            "format": "xlsx",
            "filename": "test_sheet",
            "content": {
                "sheets": [{
                    "name": "Data",
                    "headers": ["Name", "Value"],
                    "rows": [["a", 1], ["b", 2]],
                }],
            },
        })

        if result.status == ToolStatus.SUCCESS:
            assert (tmp_path / "test_sheet.xlsx").exists()
        else:
            assert "openpyxl" in result.error

    @pytest.mark.asyncio
    async def test_generate_pdf(self, tmp_path):
        with patch("aria.tools.builtin.docgen.settings") as mock_s:
            mock_s.docgen_output_dir = str(tmp_path)
            from aria.tools.builtin.docgen import DocumentGenerationTool
            tool = DocumentGenerationTool()

        result = await tool.execute({
            "format": "pdf",
            "filename": "test_pdf",
            "content": {
                "title": "Test PDF",
                "paragraphs": ["Hello PDF."],
            },
        })

        if result.status == ToolStatus.SUCCESS:
            assert (tmp_path / "test_pdf.pdf").exists()
        else:
            assert "reportlab" in result.error


# ============================================================================
# SoulTool
# ============================================================================

class TestSoulTool:
    """Tests for aria.tools.builtin.soul.SoulTool."""

    @pytest.mark.asyncio
    async def test_properties(self):
        with patch("aria.tools.builtin.soul.soul_manager"):
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
        assert tool.name == "update_soul"
        assert tool.type == ToolType.BUILTIN

    @pytest.mark.asyncio
    async def test_read(self):
        with patch("aria.tools.builtin.soul.soul_manager") as mock_sm:
            mock_sm.read.return_value = "I am ARIA."
            mock_sm.path = "/home/ben/SOUL.md"
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
            result = await tool.execute({"action": "read"})
        assert result.status == ToolStatus.SUCCESS
        assert result.output == "I am ARIA."

    @pytest.mark.asyncio
    async def test_read_empty(self):
        with patch("aria.tools.builtin.soul.soul_manager") as mock_sm:
            mock_sm.read.return_value = None
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
            result = await tool.execute({"action": "read"})
        assert result.status == ToolStatus.SUCCESS
        assert "does not exist" in result.output.lower() or "empty" in result.output.lower()

    @pytest.mark.asyncio
    async def test_write(self):
        with patch("aria.tools.builtin.soul.soul_manager") as mock_sm:
            mock_sm.write.return_value = "/home/ben/SOUL.md"
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
            result = await tool.execute({"action": "write", "content": "New identity"})
        assert result.status == ToolStatus.SUCCESS
        mock_sm.write.assert_called_once_with("New identity")

    @pytest.mark.asyncio
    async def test_write_no_content(self):
        with patch("aria.tools.builtin.soul.soul_manager"):
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
            result = await tool.execute({"action": "write"})
        assert result.status == ToolStatus.ERROR
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_unknown_action(self):
        with patch("aria.tools.builtin.soul.soul_manager"):
            from aria.tools.builtin.soul import SoulTool
            tool = SoulTool()
            result = await tool.execute({"action": "delete"})
        assert result.status == ToolStatus.ERROR
        assert "unknown" in result.error.lower()


# ============================================================================
# PiCodingAgentTool
# ============================================================================

class TestPiCodingAgentTool:
    """Tests for aria.tools.builtin.pi_coding.PiCodingAgentTool."""

    def _make_mock_db(self):
        from tests.conftest import make_mock_db
        return make_mock_db()

    @pytest.mark.asyncio
    async def test_properties(self):
        db = self._make_mock_db()
        from aria.tools.builtin.pi_coding import PiCodingAgentTool
        tool = PiCodingAgentTool(db)
        assert tool.name == "pi_coding_agent"
        assert tool.type == ToolType.BUILTIN

    @pytest.mark.asyncio
    async def test_empty_task_error(self):
        db = self._make_mock_db()
        from aria.tools.builtin.pi_coding import PiCodingAgentTool
        tool = PiCodingAgentTool(db)
        result = await tool.execute({"task": ""})
        assert result.status == ToolStatus.ERROR
        assert "required" in result.error.lower()

    @pytest.mark.asyncio
    async def test_agent_not_found(self):
        db = self._make_mock_db()
        db.agents.find_one = AsyncMock(return_value=None)
        from aria.tools.builtin.pi_coding import PiCodingAgentTool
        tool = PiCodingAgentTool(db)
        result = await tool.execute({"task": "Build a REST API"})
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.error.lower()

    @pytest.mark.asyncio
    async def test_success(self):
        from bson import ObjectId
        db = self._make_mock_db()
        agent_id = ObjectId()
        db.agents.find_one = AsyncMock(return_value={
            "_id": agent_id,
            "name": "Pi Coder",
            "slug": "pi-coding",
            "llm": {"backend": "llamacpp", "model": "qwen3", "temperature": 0.7},
        })
        db.conversations.insert_one = AsyncMock(
            return_value=MagicMock(inserted_id=ObjectId())
        )

        # Mock the orchestrator
        async def fake_process_message(*args, **kwargs):
            from aria.llm.base import StreamChunk
            yield StreamChunk(type="text", content="Here's the solution...")
            yield StreamChunk(type="done", usage={})

        mock_orchestrator = MagicMock()
        mock_orchestrator.process_message = fake_process_message

        from aria.tools.builtin.pi_coding import PiCodingAgentTool
        tool = PiCodingAgentTool(db)

        with patch("aria.core.orchestrator.Orchestrator", return_value=mock_orchestrator), \
             patch("aria.api.deps.get_tool_router", return_value=MagicMock()), \
             patch("aria.api.deps.get_task_runner", new_callable=AsyncMock, return_value=MagicMock()), \
             patch("aria.api.deps.get_coding_session_manager", new_callable=AsyncMock, return_value=MagicMock()):

            result = await tool.execute({"task": "Build a REST API"})

        assert result.status == ToolStatus.SUCCESS
        assert "solution" in result.output["response"]
        assert result.output["agent"] == "Pi Coder"


# ============================================================================
# Helpers
# ============================================================================

async def _async_iter(items):
    """Helper to create an async iterator from a list."""
    for item in items:
        yield item
