import os,sys,json,time
from pathlib import Path
os.environ['HERMES_HOME']='/Users/ben/Services/data/hermes-home'
from dotenv import load_dotenv
load_dotenv('/Users/ben/Services/data/hermes-home/.env',override=True)
os.environ['HERMES_GATEWAY_SESSION']='1'
os.environ['HERMES_INTERACTIVE']='1'
sys.path.insert(0,'/Users/ben/Services/apps/hermes-agent')
from hermes_cli.config import load_config
from hermes_cli.tools_config import _get_platform_tools
from tools.mcp_tool import discover_mcp_tools,shutdown_mcp_servers
import model_tools
from toolsets import resolve_toolset
R=Path(__file__).resolve().parent
cfg=load_config();out={'platforms':{},'auxiliary_models':{k:{p:v.get(p) for p in ['provider','model','base_url']} for k,v in cfg.get('auxiliary',{}).items() if isinstance(v,dict) and 'model' in v},'plugins_enabled':cfg.get('plugins',{}).get('enabled'),'tool_search':cfg.get('tools',{}).get('tool_search')}
try:
 start=time.monotonic();names=discover_mcp_tools(allowed_mcp_names=['aria']);out['mcp_discovery_ms']=(time.monotonic()-start)*1000;out['mcp_names']=names
 for platform in ['signal','cli']:
  enabled=sorted(_get_platform_tools(cfg,platform,include_default_mcp_servers=True));requested=set(n for ts in enabled for n in resolve_toolset(ts))
  full=model_tools.get_tool_definitions(enabled_toolsets=enabled,disabled_toolsets=cfg.get('agent',{}).get('disabled_toolsets',[]),quiet_mode=True,skip_tool_search_assembly=True)
  visible=model_tools.get_tool_definitions(enabled_toolsets=enabled,disabled_toolsets=cfg.get('agent',{}).get('disabled_toolsets',[]),quiet_mode=True)
  available={t['function']['name'] for t in full};out['platforms'][platform]={'enabled_toolsets':enabled,'full_tool_names':sorted(available),'model_visible_tools':[t['function']['name'] for t in visible],'unavailable_requested_tools':sorted(requested-available),'full_schema_chars':len(json.dumps(full)),'visible_schema_chars':len(json.dumps(visible))}
 (R/'hermes-inventory.json').write_text(json.dumps(out,indent=2)+'\n')
 print(json.dumps({k:v for k,v in out.items() if k!='mcp_names'},indent=2))
finally:shutdown_mcp_servers()
