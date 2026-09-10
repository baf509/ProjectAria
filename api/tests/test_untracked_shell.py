import json
import os
from pathlib import Path
import runpy
import subprocess

import pytest

ROOT = Path(__file__).parents[2]


def helper():
    return runpy.run_path(str(ROOT / 'scripts/aria-untracked-shell'))


@pytest.mark.parametrize('tool', ['claude', 'codex', 'pi'])
@pytest.mark.parametrize('marker', [{'TMUX': 'captured-pane'}, {'ARIA_MANAGED': '1'}])
def test_cannot_hide_inside_an_existing_captured_terminal(tmp_path, tool, marker):
    with pytest.raises(ValueError, match='normal terminal'):
        helper()['prepare'](tool, [], marker, tmp_path, tmp_path)
    assert not (tmp_path / '.local').exists()


def test_claude_disables_independent_transcript_and_mcp_capture(tmp_path):
    command, env = helper()['prepare']('claude', ['a prompt with spaces'], {
        'ARIA_API_KEY': 'not-for-child', 'ARIA_MANAGED': '0',
        'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE': '1', 'PATH': '/bin',
    }, tmp_path, tmp_path)
    assert command[-1] == 'a prompt with spaces'
    assert json.loads(command[command.index('--mcp-config') + 1]) == {'mcpServers': {}}
    assert json.loads(command[command.index('--settings') + 1])['disableAllHooks']
    assert env['CLAUDE_CODE_SKIP_PROMPT_HISTORY'] == '1'
    assert 'CLAUDE_CODE_FORCE_SESSION_PERSISTENCE' not in env
    assert 'ARIA_API_KEY' not in env
    assert env['ARIA_UNTRACKED'] == '1'


def test_codex_only_disables_existing_aria_mcp_without_changing_config(tmp_path):
    conf = tmp_path / '.codex/config.toml'
    conf.parent.mkdir()
    content = '[mcp_servers.aria]\ncommand="aria-mcp"\n[mcp_servers.docs]\nurl="https://example.test/mcp"\n'
    conf.write_text(content)
    command, _ = helper()['prepare']('codex', ['--model', 'chosen'], {}, tmp_path, tmp_path)
    assert 'mcp_servers."aria".enabled=false' in command
    assert not any('docs' in arg for arg in command)
    assert conf.read_text() == content
    conf.unlink()
    assert helper()['prepare']('codex', [], {}, tmp_path, tmp_path)[0] == ['codex']


def test_pi_keeps_gateway_model_auth_but_separates_history_and_identity(tmp_path):
    source = tmp_path / '.pi/agent'
    source.mkdir(parents=True)
    settings = {'defaultProvider': 'aria', 'defaultModel': 'chosen', 'extensions': ['tracking.js']}
    models = {'providers': {'aria': {
        'baseUrl': 'http://localhost:8200/llm/v1-identified', 'apiKey': 'ARIA_PI_KEY',
        'headers': {'X-Aria-Caller': 'desk-pi', 'X-API-Key': 'retained-auth'},
        'models': [{'id': 'chosen', 'headers': {'x-aria-session': 'old-session'}}],
    }}}
    (source / 'settings.json').write_text(json.dumps(settings))
    original = json.dumps(models)
    (source / 'models.json').write_text(original)
    command, env = helper()['prepare']('pi', ['--continue'], {'ARIA_PI_KEY': 'test-secret'}, tmp_path, tmp_path)
    private = Path(env['PI_CODING_AGENT_DIR'])
    assert private != source
    assert Path(env['PI_CODING_AGENT_SESSION_DIR']).is_relative_to(private)
    conf = json.loads((private / 'models.json').read_text())['providers']['aria']
    assert conf['baseUrl'] == 'http://localhost:8200/llm/v1'
    assert conf['apiKey'] == 'test-secret'
    assert conf['headers'] == {'X-API-Key': 'retained-auth'}
    assert conf['models'][0]['headers'] == {}
    assert json.loads((private / 'settings.json').read_text()) == {'defaultProvider': 'aria', 'defaultModel': 'chosen'}
    assert (private / 'models.json').stat().st_mode & 0o777 == 0o600
    assert (source / 'models.json').read_text() == original
    assert '--continue' in command and '--no-extensions' in command
    # An in-Pi settings change survives a later launch and remains independent
    # of the managed profile's defaults.
    (private / 'settings.json').write_text(json.dumps({'defaultThinkingLevel': 'high'}))
    helper()['prepare']('pi', [], {'ARIA_PI_KEY': 'new-test-secret'}, tmp_path, tmp_path)
    assert json.loads((private / 'settings.json').read_text())['defaultThinkingLevel'] == 'high'


@pytest.mark.parametrize('tool', ['claude', 'codex', 'pi'])
@pytest.mark.parametrize('flag', ['--no-aria', '--local'])
def test_shell_router_runs_untracked_with_literal_arguments(tmp_path, tool, flag):
    # Isolated shell account: helpers only print argv. No real CLI, tmux, API or
    # production credentials are used by this integration test.
    binary = tmp_path / '.local/bin'
    binary.mkdir(parents=True)
    stub = binary / 'aria-untracked-shell'
    stub.write_text('#!/bin/sh\nprintf "%s\\n" "$@"\nexit 17\n')
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith('ARIA_') and k not in ('TMUX', 'ZDOTDIR')}
    env['HOME'] = str(tmp_path)
    result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; shift; "$@"', 'test',
                             str(ROOT / 'scripts/aria-shells-mac.sh'), tool, flag,
                             'literal $(false) argument'], env=env, capture_output=True, text=True)
    assert result.returncode == 17, result.stderr
    assert result.stdout.splitlines() == [tool, 'literal $(false) argument']


def test_shell_router_default_still_calls_managed_launcher(tmp_path):
    config = tmp_path / '.config/aria'
    config.mkdir(parents=True)
    stub = config / 'aria-local-shell'
    stub.write_text('#!/bin/sh\nprintf "managed:%s\\n" "$1"\n')
    stub.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith('ARIA_') and k not in ('TMUX', 'ZDOTDIR')}
    env['HOME'] = str(tmp_path)
    result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; codex', 'test',
                             str(ROOT / 'scripts/aria-shells-mac.sh')], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == 'managed:codex'
