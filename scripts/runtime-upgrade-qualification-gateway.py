#!/usr/bin/env python3
"""Temporary authenticated runtime qualification; no background controllers or route changes.

Task 6aa97711c5fd25b265732665. Mac forwards: 18109 -> Corsair 18109,
18079 -> Red 8079. Gateway listens on Mac loopback 18201.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
installed = Path('/Users/ben/Services/apps/ProjectAria/current/api').resolve()
sys.path.insert(0, str(installed))
# Qualify this checkout's proxy with the installed dependencies and config.
import aria, aria.api, aria.api.routes
aria.api.routes.__path__.insert(0, str(repo/'api/aria/api/routes'))
from aria.infrastructure import model_servers

path = repo/'api/aria/infrastructure/red_paro_int5_qualification.py'
loader = importlib.util.spec_from_file_location('red_paro_int5_qualification_spec', path)
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
template = module.make_spec(model_servers.ModelServerSpec)
remote_ssh = template.remote_start_command[:-1]
candidate = replace(template,
    slug='Red-PARO-Source-3368c48-Test',
    runtime_ref='3368c488916644e5730c55a519260c51e16affb0',
    description='Temporary source-only qualification; pinned Radiance 0.9.3/vLLM 0.27.1 and existing int5 weights.',
    container_name='red-paro-source-candidate-20260915',
    remote_model_id='red-paro-source-3368c48-test',
    # Reachability replaces the remote command with 'exit', so retain the SSH
    # prefix. Lifecycle commands still fail closed; mutation routes are absent.
    remote_start_command=remote_ssh+('false',), remote_stop_command=remote_ssh+('false',),
    not_startable_reason='Temporary qualification: controlled by the bounded runtime-upgrades-20260915 test supervisor.',
    consumers_note='Task 6aa97711c5fd25b265732665; isolated mutable paths; restore incumbent after tests.')
halogen = model_servers.ModelServerSpec(
    slug='Halogen-Qwen3.8-Flash-Next-0102-Test',
    description='Temporary Halogen 0.10.2 qualification, existing W4B weights and 4x262144 geometry.',
    runtime_repo='ghcr.io/peonist-ai/halogen-flash-server',
    runtime_ref='0.10.2@sha256:a69cf58b4791294c9fcbb59e3b58bf18ef9878c2e788e9b727b3dfa70d3cfb17',
    backend_device='Strix Halo gfx1151', devices=('Strix Halo gfx1151',),
    host_machine='machine:corsair-ai', memory_pool='halo-gtt',
    runtime_family='halogen', health_timeout_s=3.0,
    container_name='halogen-flash-candidate-20260915', port=18109,
    startable=False, allow_force_start=False, auto_route=False,
    exclusive_with=('Halogen-Qwen3.8-Flash-Next-W4B-Halo','Qwen3.8-Flash-Next-CUDA-Halo-Candidate'),
    consumers_note='Task 6aa97711c5fd25b265732665; synthetic qualification only; incumbent restored afterwards.')
baseline = replace(model_servers._BY_SLUG['Red-Qwen3.8-27B-PARO-INT5'], auto_route=False, startable=False)
model_servers.REGISTRY = (candidate, halogen, baseline)
model_servers._BY_SLUG.clear()
model_servers._BY_SLUG.update({s.slug:s for s in model_servers.REGISTRY})

from aria.main import app
from aria.db.mongodb import connect_db, close_db
from aria.core import readiness


@asynccontextmanager
async def qualification_lifespan(app):
    await connect_db()
    readiness.mark_ready()
    try:
        yield
    finally:
        await close_db()


app.router.lifespan_context = qualification_lifespan
app.router.routes[:] = [route for route in app.router.routes if (
    getattr(route, 'path', '').startswith(('/llm/v1/', '/llm/v1-identified/', '/api/v1/health/'))
    or (getattr(route, 'path', '').startswith('/api/v1/infrastructure/model-servers')
        and getattr(route, 'methods', set()) == {'GET'})
)]
if __name__ == '__main__':
    import uvicorn
    uvicorn.run(app, host='127.0.0.1', port=18201, log_level='info')
