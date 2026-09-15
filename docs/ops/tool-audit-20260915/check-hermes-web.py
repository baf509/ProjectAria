import os,sys,json,asyncio,time
from pathlib import Path
os.environ['HERMES_HOME']='/Users/ben/Services/data/hermes-home'
from dotenv import load_dotenv
load_dotenv('/Users/ben/Services/data/hermes-home/.env',override=True)
sys.path.insert(0,'/Users/ben/Services/apps/hermes-agent')
from tools.web_tools import web_search_tool,web_extract_tool
from tools.browser_use_cli import _find_cli,is_browser_use_cli_mode
R=Path(__file__).resolve().parent
out={'browser_use_cli_command':_find_cli(),'browser_use_mode':is_browser_use_cli_mode()}
start=time.monotonic();search=json.loads(web_search_tool('Python official documentation',limit=2));out['search']={'elapsed_ms':(time.monotonic()-start)*1000,'response':search}
async def main():
 start=time.monotonic();result=json.loads(await web_extract_tool(['https://example.com'],char_limit=500));out['extract']={'elapsed_ms':(time.monotonic()-start)*1000,'response':result}
asyncio.run(main());(R/'hermes-web-checks.json').write_text(json.dumps(out,indent=2)+'\n');print(json.dumps(out,indent=2)[:5500])
