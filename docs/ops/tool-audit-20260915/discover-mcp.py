import asyncio,json,os,runpy,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
R=Path(__file__).resolve().parent
_,key=runpy.run_path('/Users/ben/Development/Infrastructure/ProjectAria/scripts/aria-route-task')['resolve_target'](None)
async def main():
 start=time.monotonic()
 p=StdioServerParameters(command='/Users/ben/Services/apps/bin/run-aria-mcp',env={'ARIA_API_URL':'http://127.0.0.1:8200','ARIA_API_KEY':key})
 with (R/'mcp-discovery-stderr.log').open('w') as stderr:
  async with stdio_client(p,errlog=stderr) as (rd,wr):
   async with ClientSession(rd,wr) as s:
    init=await s.initialize();tools=[];cursor=None
    while True:
     result=await s.list_tools(cursor=cursor);tools.extend([t.model_dump(mode='json') for t in result.tools]);cursor=result.nextCursor
     if not cursor:break
    out={'server':init.serverInfo.model_dump(mode='json'),'discovery_ms':(time.monotonic()-start)*1000,'tools':tools,'read_checks':{}}
    for name in ['aria_health','tool_contract_status']:
     t=time.monotonic();response=await s.call_tool(name,{})
     out['read_checks'][name]={'is_error':response.isError,'latency_ms':(time.monotonic()-t)*1000,'data':response.structuredContent or [x.text for x in response.content if x.type=='text']}
    (R/'mcp-inventory.json').write_text(json.dumps(out,indent=2)+'\n')
    print('TOOLS',len(tools),'DISCOVERY_MS',round(out['discovery_ms']));print('NAMES',[t['name'] for t in tools]);print('CHECKS',json.dumps(out['read_checks'])[:3500])
asyncio.run(asyncio.wait_for(main(),timeout=60))
