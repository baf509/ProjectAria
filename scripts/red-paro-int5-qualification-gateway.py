#!/usr/bin/env python3
"""Authenticated ARIA gateway for isolated PARO int5 qualification.

Use installed production code/configuration without starting background
controllers, migrations or changing production routing. Set
RED_PARO_INCLUDE_RADIANCE=1 for the same-gateway baseline comparison.
"""
from contextlib import asynccontextmanager
from dataclasses import replace
import importlib.util
import os
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
installed = Path('/Users/ben/Services/apps/ProjectAria/current/api')
sys.path.insert(0, str(installed))
from aria.infrastructure import model_servers

path = repo/'api/aria/infrastructure/red_paro_int5_qualification.py'
loader = importlib.util.spec_from_file_location('red_paro_int5_qualification_spec', path)
module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(module)
candidate = module.make_spec(model_servers.ModelServerSpec)
comparison = ()
if os.environ.get('RED_PARO_INCLUDE_RADIANCE') == '1':
    baseline = model_servers._BY_SLUG['Red-Qwen3.8-27B-MXFP4']
    comparison = (replace(baseline, auto_route=False, startable=False),)
model_servers.REGISTRY = (candidate,)+comparison
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
