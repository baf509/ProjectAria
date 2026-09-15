#!/usr/bin/env python3
"""Record completion only after all post-activation functional checks pass."""
from datetime import datetime, timezone
import json
from pathlib import Path
import runpy
import httpx

root = Path(__file__).resolve().parent
report = json.loads((root / 'functional-after-activation.json').read_text())
if not report.get('ok') or report['checks']['hermes_vision']['status'] != 'passed':
    raise SystemExit('Functional checks are incomplete; repair task stays active.')
_, key = runpy.run_path(str(root.parents[2] / 'scripts/aria-route-task'))['resolve_target'](None)
client = httpx.Client(base_url='http://127.0.0.1:8200', headers={'X-API-Key': key}, timeout=30)
content = (root / 'REPORT.md').read_text()
content += '\n\n## Activation verified\n\nAll post-activation functional checks passed at ' + datetime.now(timezone.utc).isoformat() + '. The API now reports the PARO steward, accepts web_fetch, and exposes healthy tool configuration. Browser, web, PDF, MCP/memory/benchmark and 3090 vision checks passed. See functional-after-activation.json and api-activation.json.\n'
(root / 'REPORT.md').write_text(content)
response = client.post('/api/v1/obsidian/publish', json={'title': 'ARIA and Hermes Tool Repairs', 'project': 'infrastructure', 'doc_type': 'Analysis', 'content': content})
response.raise_for_status()
(root / 'publication.json').write_text(json.dumps(response.json(), indent=2) + '\n')
response = client.patch('/api/v1/todos/6aa908e4b8ae5191e672e32b', json={'notes': 'Repairs activated and all functional checks passed, including 3090 vision. Report: ' + str(root / 'REPORT.md')})
response.raise_for_status()
response = client.post('/api/v1/todos/6aa908e4b8ae5191e672e32b/done', json={})
response.raise_for_status()
print('ARIA tool repairs activated, verified, published, and marked complete.')
