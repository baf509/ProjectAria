"""Regression checks for tool-name drift and the sensitive API policy."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
from fastapi import HTTPException
from aria.config import settings
from aria.tools.router import ToolRouter
from aria.tools.validation import missing_tool_names, tool_configuration_report
from aria.db.migrations import _rename_legacy_web_tool
from aria.api.routes import agents
from tests.conftest import FakeTool

class Collection:
    def __init__(self, rows):
        self.rows = rows
        self.update_one = AsyncMock()
    def find(self, *args):
        async def iterate():
            for row in self.rows:
                yield row
        return iterate()

def test_prefixes_require_registered_matches():
    assert missing_tool_names(['web_fetch', 'browser_*', 'deep_think', 'missing_*'],
                              ['web_fetch', 'browser_tabs']) == ['deep_think', 'missing_*']

@pytest.mark.asyncio
async def test_actual_web_name_executes_without_loosening_sensitive_gate():
    router = ToolRouter()
    router.register_tool(FakeTool(tool_name='web_fetch', result='public fixture'))
    result = await router.execute_tool('web_fetch', {'input': 'https://example.com'}, source='api', allow_sensitive=False)
    assert result.status.value == 'success'
    for name in ['shell', 'filesystem']:
        allowed, reason = router._is_tool_allowed(tool_name=name, allow_sensitive=False)
        assert not allowed and reason

@pytest.mark.asyncio
async def test_enabled_missing_tools_degrade_but_disabled_optional_does_not(monkeypatch):
    monkeypatch.setattr(settings, 'tool_allowed_names', ['web_fetch', 'deep_think'])
    router = ToolRouter(); router.register_tool(FakeTool(tool_name='web_fetch'))
    db = SimpleNamespace(agents=Collection([
        {'slug': 'active', 'enabled': True, 'capabilities': {'tools_enabled': True}, 'enabled_tools': ['web', 'browser_*']},
        {'slug': 'disabled', 'enabled': False, 'enabled_tools': ['deep_think']},
    ]))
    result = await tool_configuration_report(db, router)
    assert not result['ok']
    assert result['problems'] == [{'agent': 'active', 'unregistered': ['browser_*', 'web']}]
    assert result['unavailable_optional'] == ['deep_think']
    assert result['execution_tested'] is False
    db.agents.rows[0]['enabled_tools'] = ['web_fetch']
    assert (await tool_configuration_report(db, router))['ok']

@pytest.mark.asyncio
async def test_migration_preserves_custom_names_and_concurrent_updates():
    before = ['web', 'shell', 'web_fetch', 'private_plugin']
    db = SimpleNamespace(agents=Collection([{'_id': 'a', 'enabled_tools': before}]))
    await _rename_legacy_web_tool(db)
    selector, update = db.agents.update_one.call_args.args
    assert selector == {'_id': 'a', 'enabled_tools': before}
    assert update['$set']['enabled_tools'] == ['web_fetch', 'shell', 'private_plugin']

def test_agent_write_rejects_phantom_tool_names(monkeypatch):
    router = ToolRouter(); router.register_tool(FakeTool(tool_name='browser_tabs'))
    monkeypatch.setattr(agents, 'get_tool_router', lambda: router)
    agents._validate_tools(['browser_*'])
    with pytest.raises(HTTPException) as error:
        agents._validate_tools(['web'])
    assert error.value.status_code == 422
    assert error.value.detail == {'unregistered_tools': ['web']}
