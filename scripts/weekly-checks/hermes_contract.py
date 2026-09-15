#!/usr/bin/env python3
"""Exercise a Hermes integration tree using the installed upstream consumer.

Run with the Hermes interpreter and PYTHONPATH pointing to its pinned engine.
Uses a disposable HERMES_HOME, never reads personal config/history or sends a
message. For untrusted candidates this check belongs inside the frozen image.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile


def check(source, defaults_only=False):
    with tempfile.TemporaryDirectory(prefix='aria-hermes-contract-') as directory:
        home = Path(directory)
        os.environ['HERMES_HOME'] = str(home)
        os.environ['HERMES_DISABLE_MODEL_WARMUP'] = '1'
        for plugin in ('aria-continuity', 'aria-flashnext'):
            shutil.copytree(source/'plugins'/plugin, home/'plugins'/plugin)
        (home/'config.yaml').write_text(
            'plugins:\n  enabled: [aria-continuity, aria-flashnext]\n'
            'context:\n  engine: aria-continuity\n'
            'model:\n  default: Red-Qwen3.8-27B-PARO-INT5\n')
        path = home/'plugins/aria-flashnext/__init__.py'
        spec = importlib.util.spec_from_file_location('contract_defaults', path)
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        checks = []
        gateway = 'http://127.0.0.1:8200/llm/v1-identified'
        models = [plugin.MODEL, plugin.RED, plugin.PARO, plugin.NINFER, plugin.HALOGEN]
        for model in models:
            original = {'model': model, 'messages': [{'role':'user','content':'private fixture'}],
                        'tools':[{'type':'function','function':{'name':'fixture','parameters':{}}}],
                        'temperature':0.7, 'reasoning_effort':'high'}
            before = copy.deepcopy(original)
            result = plugin.defaults(original, model=model, base_url=gateway,
                                     api_mode='chat_completions', effort='none')['request']
            assert original == before
            assert result['messages'] == before['messages'] and result['tools'] == before['tools']
            assert result['temperature'] == 0.7 and result['reasoning_effort'] == 'high'
            for url in ('https://unrelated.example/v1', gateway+'.untrusted', gateway+'?redirect=evil'):
                assert plugin.defaults(original, model=model, base_url=url,
                                       api_mode='chat_completions') is None
            assert plugin.defaults(original, model='another-model', base_url=gateway,
                                   api_mode='chat_completions') is None
            assert plugin.defaults(original, model=model, base_url=gateway, api_mode='responses') is None
            checks.append('scoped-explicit-controls:'+model)
        explicit = {'model':plugin.PARO, 'extra_body':{'chat_template_kwargs':{'enable_thinking':False}}}
        result = plugin.defaults(explicit, model=plugin.PARO, base_url=gateway,
                                 api_mode='chat_completions', effort='high')['request']
        assert result['extra_body']['chat_template_kwargs']['enable_thinking'] is False
        checks.append('explicit-template-control')
        if defaults_only:
            return {'passed':True, 'checks':checks, 'check_count':len(checks),
                    'personal_data_used':False, 'messages_sent':0}
        from hermes_cli.plugins import discover_plugins, get_plugin_context_engine
        discover_plugins(force=True)
        shared = get_plugin_context_engine()
        from agent.agent_init import _select_context_engine
        engine = _select_context_engine({'context':{'engine':'aria-continuity'}})
        assert shared is not None and engine is not shared and engine.name == 'aria-continuity'
        engine.bind_session_state(session_id='qualification')
        from model_tools import _emit_post_tool_call_hook
        payload = {'todos':[
            {'id':'1','content':'Finish accepted Mac repair; target Corsair','status':'in_progress'},
            {'id':'2','content':'Cancelled experiment','status':'cancelled'},
        ]}
        _emit_post_tool_call_hook(function_name='todo_list', function_args={},
                                 result=json.dumps(payload), session_id='qualification', status='success')
        checkpoint = home/'state/task-checkpoints'/(hashlib.sha256(b'qualification').hexdigest()+'.json')
        assert json.loads(checkpoint.read_text())['todo_result'] == payload
        assert checkpoint.stat().st_mode & 0o777 == 0o600
        summary = engine._build_summary_prompt('User: status?', 1000, None, '', True)
        assert 'Finish accepted Mac repair' in summary and 'Cancelled experiment' in summary
        assert 'Accepted Work and Authorization' in summary
        checks.append('native-discovery-session-isolation-checkpoint')
        from agent.context_compressor import SUMMARY_PREFIX
        messages = [{'role':'system','content':'fixed system'},
                    {'role':'user','content':SUMMARY_PREFIX+'\nFinish accepted work'},
                    {'role':'user','content':'status?'}]
        before = copy.deepcopy(messages)
        selected = engine.select_context(messages)
        assert messages == before and selected[0] is messages[0]
        assert 'not as new authorization' in selected[1]['content']
        assert 'never revive completed or cancelled tasks' in selected[1]['content']
        assert engine.select_context(messages) == selected
        checks.append('compaction-preserves-history-and-authorization')
        return {'passed':True, 'checks':checks, 'check_count':len(checks),
                'personal_data_used':False, 'messages_sent':0}


if __name__ == '__main__':
    print(json.dumps(check(Path(sys.argv[1]).resolve(strict=True), '--defaults-only' in sys.argv[2:])))
