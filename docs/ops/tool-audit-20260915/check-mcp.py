import asyncio,json,runpy,time
from pathlib import Path
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
R=Path(__file__).resolve().parent
_,key=runpy.run_path('/Users/ben/Development/Infrastructure/ProjectAria/scripts/aria-route-task')['resolve_target'](None)
CHECKS={'list_nodes':{},'fleet_status':{},'guard_status':{},'loop_policy':{},'benchmark_catalog':{},'host_temperatures':{},'research_status':{'limit':1},'search_memory':{'query':'PARO','limit':3}}
def summarize(d):
 if isinstance(d,list):return {'type':'list','count':len(d)}
 if isinstance(d,dict):return {'type':'dict','keys':list(d),'list_counts':{k:len(v) for k,v in d.items() if isinstance(v,list)},'status_fields':{k:v for k,v in d.items() if k in ['ready','enabled','status','available','mode','retrieval_mode','total'] and isinstance(v,(str,bool,int,float,type(None)))}}
 return {'type':type(d).__name__}
async def main():
 p=StdioServerParameters(command='/Users/ben/Services/apps/bin/run-aria-mcp',env={'ARIA_API_URL':'http://127.0.0.1:8200','ARIA_API_KEY':key})
 with (R/'mcp-read-stderr.log').open('w') as log:
  async with stdio_client(p,errlog=log) as (rd,wr):
   async with ClientSession(rd,wr) as s:
    await s.initialize()
    async def one(name,args):
     start=time.monotonic()
     try:
      v=await s.call_tool(name,args);raw=v.structuredContent
      if raw is None:
       texts=[c.text for c in v.content if c.type=='text'];blocks=[json.loads(t) for t in texts];raw=blocks[0] if len(blocks)==1 else blocks
      return name,{'ok':not v.isError,'latency_ms':(time.monotonic()-start)*1000,'summary':summarize(raw)}
     except Exception as e:return name,{'ok':False,'error':type(e).__name__}
    out=dict(await asyncio.gather(*(one(n,a) for n,a in CHECKS.items())))
    (R/'mcp-read-checks.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2))
asyncio.run(asyncio.wait_for(main(),timeout=60))
