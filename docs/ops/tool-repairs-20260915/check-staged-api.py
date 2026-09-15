import asyncio,runpy,json,sys
from pathlib import Path
from types import SimpleNamespace
sys.path.insert(0,'/Users/ben/Development/Infrastructure/ProjectAria-tool-repairs/api')
import httpx
from fastapi import FastAPI
from aria.api.routes.tools import router as routes
from aria.api.deps import get_db,get_tool_router
from aria.tools.router import ToolRouter
from aria.tools.builtin import WebTool
from aria.config import settings
_,key=runpy.run_path('/Users/ben/Development/Infrastructure/ProjectAria/scripts/aria-route-task')['resolve_target'](None)
c=httpx.Client(base_url='http://127.0.0.1:8200',headers={'X-API-Key':key},timeout=30)
rows=c.get('/api/v1/agents').json();names=[x['name'] for x in c.get('/api/v1/tools').json()]
class Collection:
 def find(self,*args):
  async def it():
   for row in rows:yield row
  return it()
r=ToolRouter();r.register_tool(WebTool())
# Discovery snapshot only; other tools are never executed in this isolated app.
for name in names:
 if name!='web_fetch':r._tools[name]=SimpleNamespace(name=name)
app=FastAPI();app.include_router(routes,prefix='/api/v1')
app.dependency_overrides[get_db]=lambda:SimpleNamespace(agents=Collection())
app.dependency_overrides[get_tool_router]=lambda:r
async def main():
 async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://staged') as client:
  health=await client.get('/api/v1/tools/health');health.raise_for_status();assert health.json()['ok'],health.text
  fetch=await client.post('/api/v1/tools/execute',json={'tool_name':'web_fetch','arguments':{'url':'https://example.com'},'timeout':20});fetch.raise_for_status();j=fetch.json();assert j['status']=='success' and 'Example Domain' in str(j['output']),j
  assert not settings.tool_api_sensitive_enabled
  out={'mode':'isolated ASGI with live read-only agent/tool inventory snapshot; no application lifespan or service starts','tool_health':health.json(),'web_fetch':{'status':j['status'],'expected_text':True},'steward_default':settings.steward_model,'sensitive_api_enabled':settings.tool_api_sensitive_enabled}
  Path('/Users/ben/Development/Infrastructure/ProjectAria-tool-repairs/docs/ops/tool-repairs-20260915/staged-api-check.json').write_text(json.dumps(out,indent=2));print('Staged API tool health and actual web_fetch passed; sensitive API remains disabled')
asyncio.run(main())
