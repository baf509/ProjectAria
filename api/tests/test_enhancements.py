"""Tests for enhancements 2-10: shell sandboxing, secret scrubbing, tool validation, circuit breaker, logging."""

import asyncio
import logging
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.core.logging import SecretScrubFilter, scrub_secrets, JSONFormatter
from aria.core.resilience import CircuitBreaker
from aria.tools.base import BaseTool, ToolParameter, ToolResult, ToolStatus, ToolType
from aria.tools.builtin.shell import ShellTool


# ---------------------------------------------------------------------------
# Enhancement 2: Shell Tool Sandboxing
# ---------------------------------------------------------------------------

class TestShellSandboxing:
    def _make_shell(self, allowed=None, denied=None):
        return ShellTool(allowed_commands=allowed, denied_commands=denied or [])

    def test_blocks_pipe(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("ls | grep foo")
        assert not ok
        assert "pipe" in err.lower() or "|" in err

    def test_blocks_semicolon(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("echo hi; rm -rf /")
        assert not ok

    def test_blocks_and_chain(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("true && rm -rf /")
        assert not ok

    def test_blocks_or_chain(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("false || echo pwned")
        assert not ok

    def test_blocks_backtick_substitution(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("echo `whoami`")
        assert not ok

    def test_blocks_dollar_paren_substitution(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("echo $(whoami)")
        assert not ok

    def test_blocks_variable_expansion(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("echo ${HOME}")
        assert not ok

    def test_blocks_output_redirect(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("echo hacked > /etc/passwd")
        assert not ok

    def test_blocks_newline_injection(self):
        shell = self._make_shell()
        ok, err = shell._validate_command("ls\nrm -rf /")
        assert not ok

    def test_allows_safe_command(self):
        shell = self._make_shell(allowed=["ls", "cat", "git status"])
        ok, err = shell._validate_command("ls -la")
        assert ok

    def test_allows_safe_command_with_allowlist(self):
        shell = self._make_shell(allowed=["git status", "git diff"])
        ok, err = shell._validate_command("git status")
        assert ok

    def test_denied_still_works(self):
        shell = self._make_shell(denied=["rm", "sudo"])
        ok, err = shell._validate_command("rm -rf /")
        # Will be caught by injection patterns first (no pipe needed)
        # or by denied list
        assert not ok


# ---------------------------------------------------------------------------
# Enhancement 3: Secret Scrubbing
# ---------------------------------------------------------------------------

class TestSecretScrubbing:
    def test_scrubs_openai_key(self):
        text = "Using key sk-abcdefghijklmnopqrstuvwxyz1234567890ab"
        result = scrub_secrets(text)
        assert "abcdefghijklmnop" not in result
        assert "REDACTED" in result

    def test_scrubs_anthropic_key(self):
        text = "key: sk-ant-xxxxxxxxxxxxxxxxxxxx"
        result = scrub_secrets(text)
        assert "xxxxxxxxxxxxxxxxxxxx" not in result

    def test_scrubs_mongodb_credentials(self):
        text = "connecting to mongodb://admin:secretpass@host:27017/db"
        result = scrub_secrets(text)
        assert "secretpass" not in result

    def test_scrubs_password_in_key_value(self):
        text = "password=my_super_secret_pw"
        result = scrub_secrets(text)
        assert "my_super_secret_pw" not in result
        assert "REDACTED" in result

    def test_scrubs_bearer_token(self):
        text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.test"
        result = scrub_secrets(text)
        assert "eyJhbGciOiJIUzI1NiJ9" not in result

    def test_preserves_normal_text(self):
        text = "Processing message for conversation abc123"
        result = scrub_secrets(text)
        assert result == text

    def test_filter_modifies_log_record(self):
        filt = SecretScrubFilter()
        record = logging.LogRecord(
            name="test", level=logging.INFO, pathname="", lineno=0,
            msg="API key is sk-abcdefghijklmnopqrstuv1234567890", args=(), exc_info=None,
        )
        filt.filter(record)
        assert "REDACTED" in record.msg
        assert "abcdefghijklmnop" not in record.msg


# ---------------------------------------------------------------------------
# Enhancement 5: Circuit Breaker
# ---------------------------------------------------------------------------

class TestCircuitBreakerIntegration:
    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        assert await cb.allow_request()
        for _ in range(3):
            await cb.record_failure()
        assert not await cb.allow_request()

    @pytest.mark.asyncio
    async def test_circuit_resets_on_success(self):
        cb = CircuitBreaker(failure_threshold=3, recovery_timeout_seconds=60)
        await cb.record_failure()
        await cb.record_failure()
        await cb.record_success()
        # After success, count resets — should still be open
        assert await cb.allow_request()

    @pytest.mark.asyncio
    async def test_call_wraps_operation(self):
        cb = CircuitBreaker(failure_threshold=3)

        async def return_42():
            return 42

        result = await cb.call(return_42)
        assert result == 42

    @pytest.mark.asyncio
    async def test_call_records_failure(self):
        cb = CircuitBreaker(failure_threshold=2)

        async def fail():
            raise ValueError("boom")

        for _ in range(2):
            with pytest.raises(ValueError):
                await cb.call(fail)

        assert not await cb.allow_request()


# ---------------------------------------------------------------------------
# Enhancement 6: Tool Input Schema Validation
# ---------------------------------------------------------------------------

class _DummyTool(BaseTool):
    @property
    def name(self): return "test_tool"
    @property
    def description(self): return "test"
    @property
    def type(self): return ToolType.BUILTIN
    @property
    def parameters(self):
        return [
            ToolParameter(name="query", type="string", description="search query", required=True),
            ToolParameter(name="count", type="number", description="result count", required=False),
            ToolParameter(name="format", type="string", description="output format",
                          required=False, enum=["json", "text"]),
        ]
    async def execute(self, arguments): return ToolResult(tool_name=self.name, status=ToolStatus.SUCCESS)


class TestToolValidation:
    @pytest.mark.asyncio
    async def test_valid_args(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": "hello"})
        assert ok

    @pytest.mark.asyncio
    async def test_missing_required(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({})
        assert not ok
        assert "query" in err

    @pytest.mark.asyncio
    async def test_unknown_param(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": "hi", "bogus": True})
        assert not ok
        assert "bogus" in err

    @pytest.mark.asyncio
    async def test_wrong_type_string_as_number(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": "hi", "count": "not_a_number"})
        assert not ok
        assert "wrong type" in err.lower()

    @pytest.mark.asyncio
    async def test_wrong_type_number_as_string(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": 123})
        assert not ok
        assert "wrong type" in err.lower()

    @pytest.mark.asyncio
    async def test_enum_valid(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": "hi", "format": "json"})
        assert ok

    @pytest.mark.asyncio
    async def test_enum_invalid(self):
        tool = _DummyTool()
        ok, err = await tool.validate_arguments({"query": "hi", "format": "xml"})
        assert not ok
        assert "not in allowed values" in err


# ---------------------------------------------------------------------------
# Enhancement 7: JSON Formatter
# ---------------------------------------------------------------------------

class TestJSONFormatter:
    def test_produces_valid_json(self):
        import json
        formatter = JSONFormatter()
        record = logging.LogRecord(
            name="aria.test", level=logging.INFO, pathname="test.py", lineno=42,
            msg="Hello world", args=(), exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["level"] == "INFO"
        assert parsed["message"] == "Hello world"
        assert parsed["logger"] == "aria.test"
        assert "timestamp" in parsed
