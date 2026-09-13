#!/usr/bin/env python3
"""Run the existing authenticated ARIA gateway for the isolated GPTQ candidate.

Use the installed ARIA interpreter and configuration; imports use the installed
release, with only the candidate spec read from this canonical source tree.
No background controllers, migrations, startup hooks, or production routes
are started. The normal gateway records requests in the existing usage store.
"""
from contextlib import asynccontextmanager
import importlib.util
from pathlib import Path
import sys

repo = Path(__file__).resolve().parents[1]
installed = Path('/Users/ben/Services/apps/ProjectAria/current/api')
sys.path.insert(0, str(installed))

from aria.infrastructure import model_servers

path = repo / 'api/aria/infrastructure/red_gptq_qualification.py'
loader = importlib.util.spec_from_file_location('red_gptq_qualification_spec', path)
candidate_module = importlib.util.module_from_spec(loader)
loader.loader.exec_module(candidate_module)
candidate = candidate_module.make_spec(model_servers.ModelServerSpec)
model_servers.REGISTRY = (candidate,)
model_servers._BY_SLUG.clear()
model_servers._BY_SLUG[candidate.slug] = candidate

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
    uvicorn.run(app, host='127.0.0.1', port=18200, log_level='info')
