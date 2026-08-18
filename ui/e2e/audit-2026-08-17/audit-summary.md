# Measured responsive audit of the live ARIA web UI (http://localhost:3000, headless Chromium 151, Playwright 1.62)

Viewports: iphone-se-375 (375x667, mobile emulation), iphone-390 (390x844, mobile), ipad-768 (768x1024, mobile), laptop-1280 (1280x800). Light scheme unless noted.
Metrics: ovf = document scrollWidth beyond innerWidth (real horizontal overflow, in CSS px). small = interactive elements with any side < 44px / total interactive. under12 = text-bearing elements with computed font-size < 12px / total. nodes = DOM element count. api = XHR/fetch calls to :8200 on load.

| route | viewport | ovf px | small targets | fonts <12px | min font | DOM nodes | API calls on load | selects |
|---|---|---|---|---|---|---|---|---|
| / | iphone-se-375 | 0 | 8/12 | 24/44 | 10 | 90 | 2 | 0 |
| /inbox | iphone-se-375 | 0 | 67/67 | 247/316 | 10 | 575 | 4 | 0 |
| /chat | iphone-se-375 | 0 | 10/12 | 9/34 | 10 | 80 | 3 | 2 |
| /cockpit | iphone-se-375 | 107 | 72/72 | 267/349 | 10 | 856 | 1 | 0 |
| /cockpit/aria | iphone-se-375 | 0 | 8/8 | 24/65 | 9 | 119 | 1 | 0 |
| /operate | iphone-se-375 | 55 | 58/58 | 140/227 | 10 | 476 | 9 | 0 |
| /dashboard | iphone-se-375 | 9 | 73/86 | 36/291 | 10 | 475 | 15 | 4 |
| /dashboard/shells | iphone-se-375 | 0 | 26/687 | 1996/3358 | 9 | 6046 | 6 | 0 |
| /dashboard/benchmarks | iphone-se-375 | 0 | 36/46 | 9/122 | 10 | 231 | 4 | 0 |
| /autonomy | iphone-se-375 | 0 | 14/14 | 162/232 | 10 | 387 | 6 | 0 |
| / | iphone-390 | 0 | 8/12 | 24/44 | 10 | 90 | 2 | 0 |
| /inbox | iphone-390 | 0 | 67/67 | 247/316 | 10 | 575 | 4 | 0 |
| /chat | iphone-390 | 0 | 10/12 | 9/34 | 10 | 80 | 3 | 2 |
| /cockpit | iphone-390 | 92 | 72/72 | 267/349 | 10 | 856 | 1 | 0 |
| /cockpit/aria | iphone-390 | 0 | 8/8 | 24/65 | 9 | 119 | 1 | 0 |
| /operate | iphone-390 | 40 | 58/58 | 140/227 | 10 | 476 | 9 | 0 |
| /dashboard | iphone-390 | 0 | 73/86 | 36/291 | 10 | 475 | 15 | 4 |
| /dashboard/shells | iphone-390 | 0 | 26/687 | 1996/3358 | 9 | 6046 | 6 | 0 |
| /dashboard/benchmarks | iphone-390 | 0 | 36/46 | 9/122 | 10 | 231 | 4 | 0 |
| /autonomy | iphone-390 | 0 | 14/14 | 162/232 | 10 | 387 | 6 | 0 |
| / | ipad-768 | 0 | 8/12 | 24/44 | 10 | 90 | 2 | 0 |
| /inbox | ipad-768 | 0 | 67/67 | 247/316 | 10 | 575 | 4 | 0 |
| /chat | ipad-768 | 0 | 13/13 | 9/34 | 10 | 80 | 3 | 2 |
| /cockpit | ipad-768 | 0 | 72/72 | 267/349 | 10 | 856 | 1 | 0 |
| /cockpit/aria | ipad-768 | 0 | 8/8 | 24/65 | 9 | 119 | 1 | 0 |
| /operate | ipad-768 | 0 | 58/58 | 140/227 | 10 | 476 | 9 | 0 |
| /dashboard | ipad-768 | 0 | 73/86 | 36/291 | 10 | 475 | 15 | 4 |
| /dashboard/shells | ipad-768 | 0 | 26/687 | 2004/3358 | 9 | 6046 | 6 | 0 |
| /dashboard/benchmarks | ipad-768 | 0 | 38/46 | 9/122 | 10 | 231 | 4 | 0 |
| /autonomy | ipad-768 | 0 | 14/14 | 162/232 | 10 | 387 | 6 | 0 |
| / | laptop-1280 | 0 | 2/12 | 24/44 | 10 | 90 | 2 | 0 |
| /inbox | laptop-1280 | 0 | 61/67 | 247/316 | 10 | 575 | 4 | 0 |
| /chat | laptop-1280 | 0 | 7/13 | 9/34 | 10 | 80 | 3 | 2 |
| /cockpit | laptop-1280 | 0 | 66/72 | 267/349 | 10 | 856 | 1 | 0 |
| /cockpit/aria | laptop-1280 | 0 | 2/8 | 24/65 | 9 | 119 | 1 | 0 |
| /operate | laptop-1280 | 0 | 52/58 | 140/227 | 10 | 476 | 9 | 0 |
| /dashboard | laptop-1280 | 0 | 67/86 | 36/291 | 10 | 475 | 15 | 4 |
| /dashboard/shells | laptop-1280 | 0 | 20/687 | 2004/3358 | 9 | 6046 | 6 | 0 |
| /dashboard/benchmarks | laptop-1280 | 0 | 31/46 | 9/122 | 10 | 231 | 4 | 0 |
| /autonomy | laptop-1280 | 0 | 8/14 | 162/232 | 10 | 387 | 6 | 0 |

## Per-route detail at iphone-390 light
### /
- API URLs on load: ['/api/v1/health', '/api/v1/infrastructure/model-servers']
- font histogram (px:count): {'10.0': 12, '11.0': 12, '12.0': 15, '16.0': 5}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 1
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('a', 46, 17, 'Operate')]

### /inbox
- API URLs on load: ['/api/v1/alerts?unacked_only=true&limit=50', '/api/v1/todos?status=proposed', '/api/v1/dreams/soul-proposals', '/api/v1/shared/review']
- font histogram (px:count): {'10.0': 66, '11.0': 181, '12.0': 64, '16.0': 5}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 24
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('button', 49, 31, 'Ack'), ('button', 49, 31, 'Ack'), ('button', 49, 31, 'Ack'), ('button', 49, 31, 'Ack'), ('button', 49, 31, 'Ack')]

### /chat
- API URLs on load: ['/api/v1/agents', '/api/v1/conversations?limit=20', '/api/v1/conversations/6a6c088b47a6a642362e0a8e']
- font histogram (px:count): {'10.0': 6, '11.0': 3, '12.0': 7, '13.0': 4, '14.0': 9, '16.0': 5}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 1
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('select', 316, 36, 'code Pi Coding Agent (Ridge)se'), ('select', 276, 36, 'pi-code: You are the Pi Coding'), ('button', 74, 36, '+ New')]

### /cockpit
- API URLs on load: ['/api/v1/projects/overview']
- font histogram (px:count): {'10.0': 136, '11.0': 131, '12.0': 8, '14.0': 4, '16.0': 5, '20.0': 64, '30.0': 1}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 0
- elements extending past the viewport (top offenders): [{'tag': 'div', 'cls': 'group cursor-pointer rounded-3xl border bg-panel p-5 text-left transition hover:border-accent sm:p-6 border-line ', 'w': 450, 'right': 482, 'left': 32, 'ovx': 'visible', 'ws': 'normal', 'text': 'Hermesfocus1 alertmaster21d ago'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('a', 51, 20, '← Home'), ('button', 55, 21, 'focus'), ('button', 55, 21, 'focus'), ('button', 55, 21, 'focus'), ('button', 55, 21, 'focus')]

### /cockpit/aria
- API URLs on load: ['/api/v1/projects/aria/cockpit']
- font histogram (px:count): {'9.0': 5, '10.0': 15, '11.0': 4, '12.0': 21, '14.0': 11, '16.0': 5, '24.0': 3, '30.0': 1}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 0
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('a', 118, 20, '← All projects')]

### /operate
- API URLs on load: ['/api/v1/infrastructure/model-servers', '/api/v1/infrastructure/llm-route', '/api/v1/infrastructure/services', '/api/v1/infrastructure/model-servers/utilization', '/api/v1/benchmarks/runs?limit=25']
- font histogram (px:count): {'10.0': 57, '11.0': 83, '12.0': 79, '13.0': 2, '16.0': 6}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 6
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('button', 356, 33, 'DS4-0731-IQ2M-DSpark-64k109.0'), ('button', 356, 33, 'Ling-3.0-flash-Q6_K105.0'), ('button', 356, 33, 'DS4-0731-UD-IQ3-S-Dual-Vulkan-'), ('button', 356, 33, 'DS4-0731-ROCmFPX-Affine-Qualit'), ('button', 356, 33, 'DS4-0731-ROCMFPX-affine-256k10')]

### /dashboard
- API URLs on load: ['/api/v1/infrastructure/model-servers/runtimes', '/api/v1/agents', '/api/v1/memories?limit=50', '/api/v1/research', '/api/v1/conversations?limit=50', '/api/v1/usage/summary?days=7', '/api/v1/usage/by-agent?days=7', '/api/v1/usage/by-model?days=7', '/api/v1/tasks', '/api/v1/infrastructure/model-servers', '/api/v1/workflows', '/api/v1/admin/audit?hours=24&limit=50', '/api/v1/admin/cutover', '/api/v1/todos?status=proposed%2Cactive', '/api/v1/projects']
- font histogram (px:count): {'10.0': 33, '11.0': 3, '12.0': 87, '14.0': 156, '16.0': 5, '18.0': 4, '20.0': 1, '24.0': 1, '30.0': 1}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}, {'tag': 'div', 'cls': 'mb-6 -mx-4 flex gap-2 overflow-x-auto px-4 pb-1 sm:mx-0 sm:mb-8 sm:flex-wrap sm:', 'sw': 1102, 'cw': 358}]
- truncated (ellipsis actually clipping) elements: 47
- elements extending past the viewport (top offenders): [{'tag': 'button', 'cls': 'shrink-0 rounded-full border px-4 py-2 text-sm capitalize transition border-line bg-panel text-ink-dim hover:border-line', 'w': 144, 'right': 655, 'left': 512, 'ovx': 'visible', 'ws': 'normal', 'text': 'conversations'}, {'tag': 'a', 'cls': 'rounded-full border border-line bg-panel px-4 py-2 text-sm capitalize text-ink-dim transition hover:border-line', 'w': 118, 'right': 1102, 'left': 983, 'ovx': 'visible', 'ws': 'normal', 'text': 'benchmarks'}, {'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('button', 85, 38, 'agents'), ('button', 101, 38, 'memories'), ('button', 76, 38, 'tasks'), ('button', 101, 38, 'research'), ('button', 76, 38, 'usage')]

### /dashboard/shells
- API URLs on load: ['/api/v1/shells', '/api/v1/shells/claude-ProjectAria/events?limit=500', '/api/v1/shells/claude-ProjectAria/snapshot', '/api/v1/shells/claude-ProjectAria/stream?api_key=REDACTED_API_KEY']
- font histogram (px:count): {'9.0': 661, '10.0': 1332, '11.0': 3, '12.0': 671, '14.0': 685, '16.0': 5, '20.0': 1}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}, {'tag': 'div', 'cls': 'absolute inset-0 overflow-auto bg-ground font-mono text-xs px-4 py-3', 'sw': 1531, 'cw': 358}]
- truncated (ellipsis actually clipping) elements: 18
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('button', 136, 34, '+ New Session'), ('a', 93, 36, '← Dashboard'), ('input', 334, 34, 'Filter by name, project, tag…'), ('button', 85, 34, 'all 661'), ('button', 93, 34, 'active 2')]

### /dashboard/benchmarks
- API URLs on load: ['/api/v1/benchmarks/health', '/api/v1/benchmarks/runs?limit=25', '/api/v1/benchmarks/suites', '/api/v1/benchmarks/targets']
- font histogram (px:count): {'10.0': 6, '11.0': 3, '12.0': 65, '13.0': 1, '14.0': 41, '16.0': 5, '20.0': 1}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 0
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('a', 85, 39, '← dashboard'), ('input', 13, 13, ''), ('input', 13, 13, ''), ('input', 13, 13, ''), ('input', 13, 13, '')]

### /autonomy
- API URLs on load: ['/api/v1/dreams/status', '/api/v1/awareness/status', '/api/v1/heartbeat/status', '/api/v1/dreams/journal?limit=10', '/api/v1/awareness/observations?limit=30', '/api/v1/dreams/soul-proposals']
- font histogram (px:count): {'10.0': 33, '11.0': 129, '12.0': 65, '16.0': 5}
- horizontal scroll containers with real overflow: [{'tag': 'ul', 'cls': 'flex list-none gap-1 overflow-x-auto px-2 pb-2 lg:flex-col lg:gap-0.5 lg:overflo', 'sw': 488, 'cw': 390}]
- truncated (ellipsis actually clipping) elements: 32
- elements extending past the viewport (top offenders): [{'tag': 'li', 'cls': '', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'normal', 'text': 'Autonomyawareness, dreams'}, {'tag': 'a', 'cls': 'block whitespace-nowrap rounded-sm border-l-2 px-3 py-2 transition-colors lg:whitespace-normal focus-visible:outline foc', 'w': 84, 'right': 480, 'left': 396, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomyawareness, dreams'}, {'tag': 'span', 'cls': 'block text-xs', 'w': 58, 'right': 468, 'left': 410, 'ovx': 'visible', 'ws': 'nowrap', 'text': 'Autonomy'}]
- smallest tap targets: [('a', 34, 17, 'ARIA'), ('a', 62, 32, 'Inboxwaiting on you'), ('a', 84, 32, 'Conversechat, voice, history'), ('a', 91, 32, 'Supervisesessions, shells, age'), ('a', 77, 32, 'Operatemodels, benchmarks, hea'), ('a', 55, 32, 'Knowmemory, research, projects'), ('a', 84, 32, 'Autonomyawareness, dreams'), ('button', 79, 31, 'Approve'), ('button', 71, 31, 'Reject'), ('button', 124, 31, 'Trigger dream'), ('button', 86, 31, 'Poll now'), ('button', 79, 31, 'Analyse')]

## Visual observations from screenshots (iphone-390 light)
- /cockpit: project cards are 450px wide in a 390px viewport (grid min column ~ 420-450px). The nav bar and page header end at 390px, the cards extend to 482px, so a blank strip appears at the right of the header/nav — this is the 'blank white sections' symptom Ben reports. Serif display heading, `rounded-3xl` cards: a different design language from /operate.
- /operate: stacks into one column; the status stats in the page header wrap into 4 rows on the phone; fleet list rows are 33px tall; 40px horizontal overflow somewhere below the fold. Desktop /operate is the canonical 'instrument panel' look (mono, tokens, 3 columns).
- /dashboard: 'OPERATIONS CONSOLE / ARIA Dashboard' serif heading, 10 pill tabs in a horizontal scroller (1102px of tabs in 358px), agent cards stacked; 15 API calls fire on load (all tabs eager). Old `primary-*` palette.
- /dashboard/shells: light mode renders several buttons with INVISIBLE labels ('+ New Session', the 'all' filter chip, the 'SNAPSHOT' tab appear as pink boxes with no text) — a light-theme contrast defect. Purple/fuchsia accent, another design language. Terminal pane is absolute inset-0 with 1531px scrollWidth (no wrapping) and 6046 DOM nodes.
- /inbox: fits, but nearly all text is 10-11px mono; alert titles are truncated with ellipsis and the message needs a tap to read.
- /chat: fits (100dvh flush surface); the agent <select> label reads 'code Pi Coding Agent (Ridge)' (icon NAME leaking into the label).
- The AppShell nav is a horizontal scroller (488px of items in 390px): 'Know' is cut in half and 'Autonomy' is off-screen with no scroll affordance; nav links are 32px tall.
- WebKit could not be launched (missing system deps), so iOS-Safari-specific behaviour (e.g. body overflow-x:hidden not preventing viewport panning, 100vh vs the URL bar) is not measured here.