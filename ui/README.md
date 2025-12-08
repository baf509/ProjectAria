# ARIA Web UI

Modern web interface for ARIA built with Next.js 14 and TypeScript.

## Features

- **Real-time Chat** - Streaming responses from AI agents
- **Conversation Management** - Create, view, and switch between conversations
- **Responsive Design** - Works on desktop and mobile
- **Dark Mode** - Automatic dark mode support
- **Type-safe** - Full TypeScript support with proper typing

## Tech Stack

- **Next.js 14** - React framework with App Router
- **TypeScript** - Type-safe development
- **Tailwind CSS** - Utility-first CSS framework
- **Lucide React** - Icon library
- **Axios** - HTTP client for API communication

## Development

### Prerequisites

- Node.js 20+
- npm or yarn

### Setup

```bash
# Install dependencies
npm install

# Run development server
npm run dev
```

The UI will be available at http://localhost:3000

### Environment Variables

Create a `.env.local` file:

```bash
NEXT_PUBLIC_API_URL=http://localhost:8000
```

## Docker

### Build

```bash
docker build -t aria-ui .
```

### Run

```bash
docker run -p 3000:3000 -e NEXT_PUBLIC_API_URL=http://localhost:8000 aria-ui
```

Or use docker-compose from the project root:

```bash
docker compose up ui
```

## Project Structure

```
ui/
├── src/
│   ├── app/           # Next.js app router pages
│   │   ├── page.tsx   # Home page (redirects to chat)
│   │   └── chat/
│   │       └── page.tsx  # Main chat interface
│   ├── components/    # Reusable React components (future)
│   ├── lib/
│   │   └── api-client.ts  # ARIA API client
│   └── types/
│       └── index.ts   # TypeScript type definitions
├── public/            # Static assets
├── package.json
├── tsconfig.json
├── tailwind.config.js
└── next.config.js
```

## API Client

The `apiClient` in `src/lib/api-client.ts` provides type-safe methods for all ARIA API endpoints:

- **Health**: `checkHealth()`, `checkLLMHealth()`
- **Conversations**: `listConversations()`, `getConversation()`, `createConversation()`, `streamMessage()`
- **Agents**: `listAgents()`, `getAgent()`, `createAgent()`, `updateAgent()`
- **Memories**: `listMemories()`, `searchMemories()`, `createMemory()`
- **Tools**: `listTools()`, `getTool()`, `executeTool()`
- **MCP**: `listMCPServers()`, `addMCPServer()`, `removeMCPServer()`

## Future Enhancements

- Agent management UI
- Memory browser/viewer
- Tool execution visualization
- Settings/configuration page
- Multi-agent support
- File upload for document chat
- Voice input/output

## License

Part of the ARIA project.
