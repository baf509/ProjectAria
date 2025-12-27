# GLM-4.7 Setup Guide for ARIA

This guide shows how to use GLM-4.7 (hosted on DeepInfra) via OpenRouter with ARIA.

## About GLM-4.7

- **Provider**: DeepInfra (accessed via OpenRouter)
- **Model ID**: `z-ai/glm-4.7`
- **Context Window**: ~128k tokens
- **Strengths**: Good multilingual support (especially Chinese/English), cost-effective, fast inference
- **Pricing**: Check [OpenRouter pricing](https://openrouter.ai/models/z-ai/glm-4.7) for current rates

## Quick Start

### 1. Prerequisites

```bash
# Install jq for JSON parsing (if not already installed)
sudo pacman -S jq  # or: sudo apt install jq

# Make sure Docker is running
sudo systemctl start docker
```

### 2. Configure OpenRouter API Key

```bash
# Add your OpenRouter API key to .env
echo "OPENROUTER_API_KEY=sk-or-v1-YOUR-KEY-HERE" >> .env

# Verify it's set
grep OPENROUTER_API_KEY .env
```

Get your API key from: https://openrouter.ai/keys

### 3. Start ARIA with GLM-4.7

```bash
# Run the automated setup script
./start-aria-glm4.sh
```

This script will:
- ✅ Start all Docker services
- ✅ Verify OpenRouter is configured
- ✅ Create a GLM-4.7 agent
- ✅ Create a conversation ready to use
- ✅ Display your conversation ID

## Manual Setup (Alternative)

If you prefer to set things up manually:

### 1. Start Services

```bash
docker compose up -d
```

### 2. Verify OpenRouter

```bash
curl http://localhost:8000/api/v1/health/llm | jq '.[] | select(.backend=="openrouter")'
```

Should return:
```json
{
  "backend": "openrouter",
  "available": true,
  "reason": "OpenRouter API configured"
}
```

### 3. Create GLM-4.7 Agent

```bash
./create-glm4-agent.sh
```

Or manually:
```bash
curl -X POST http://localhost:8000/api/v1/agents \
  -H "Content-Type: application/json" \
  -d '{
    "name": "GLM-4.7 Assistant",
    "slug": "glm4",
    "description": "AI assistant powered by GLM-4.7 via OpenRouter/DeepInfra",
    "system_prompt": "You are a helpful AI assistant powered by GLM-4.7. Be concise, accurate, and helpful.",
    "llm": {
      "backend": "openrouter",
      "model": "z-ai/glm-4.7",
      "temperature": 0.7,
      "max_tokens": 4096
    },
    "capabilities": {
      "memory_enabled": true,
      "tools_enabled": true,
      "computer_use_enabled": false
    },
    "memory_config": {
      "auto_extract": true,
      "short_term_messages": 20,
      "long_term_results": 10
    },
    "enabled_tools": []
  }' | jq
```

### 4. Create a Conversation

```bash
curl -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d '{"agent_slug": "glm4", "title": "My GLM-4.7 Chat"}' | jq -r '.id'
```

Save the conversation ID returned.

## Using GLM-4.7

### Via CLI

```bash
# Install CLI (first time only)
cd cli && pip install -e . && cd ..

# Chat with your conversation
aria chat --conversation CONV_ID "Hello! Tell me about yourself."

# List all agents
aria agents list
```

### Via Web UI

1. Open http://localhost:3000
2. Select "GLM-4.7 Assistant" from the agent dropdown
3. Start chatting!

### Via API (curl)

```bash
# Stream a message
curl -N -X POST http://localhost:8000/api/v1/conversations/CONV_ID/messages \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"content": "What can you help me with?", "stream": true}'
```

## Advanced Configuration

### Make GLM-4.7 the Default Agent

```bash
# Get the agent ID
AGENT_ID=$(curl -s http://localhost:8000/api/v1/agents | jq -r '.[] | select(.slug=="glm4") | .id')

# Set as default
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{"is_default": true}' | jq
```

### Adjust Temperature

Lower temperature (0.3-0.5) for more focused, deterministic responses:
```bash
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{"llm": {"temperature": 0.3}}' | jq
```

Higher temperature (0.8-1.0) for more creative responses:
```bash
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{"llm": {"temperature": 0.9}}' | jq
```

### Enable Tools

To allow GLM-4.7 to use filesystem, shell, and web tools:

```bash
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{
    "enabled_tools": ["filesystem", "shell", "web_fetch"],
    "capabilities": {
      "tools_enabled": true
    }
  }' | jq
```

## Cost Optimization

### Use Streaming for Better UX

Streaming provides faster perceived response time without extra cost:
```bash
# Always use stream: true in your requests
{"content": "Your message", "stream": true}
```

### Set max_tokens Appropriately

Limit token usage for shorter responses:
```bash
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{"llm": {"max_tokens": 2048}}' | jq
```

## Troubleshooting

### Check OpenRouter Status

```bash
curl http://localhost:8000/api/v1/health/llm | jq
```

### View API Logs

```bash
docker compose logs -f api
```

### Test OpenRouter Directly

```bash
curl https://openrouter.ai/api/v1/chat/completions \
  -H "Authorization: Bearer $OPENROUTER_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "z-ai/glm-4.7",
    "messages": [{"role": "user", "content": "Hello!"}]
  }'
```

### Common Issues

**Issue**: "OpenRouter API key not configured"
```bash
# Solution: Check .env file
cat .env | grep OPENROUTER_API_KEY

# Restart services after updating .env
docker compose restart api
```

**Issue**: Rate limit errors
```bash
# Solution: Check your OpenRouter credits/limits at https://openrouter.ai
# Consider adding a fallback model in the agent configuration
```

**Issue**: Model not found
```bash
# Solution: Verify model name is exactly "z-ai/glm-4.7"
# Check OpenRouter's model list: https://openrouter.ai/models
```

## Model Alternatives

If you want to try other models via OpenRouter, here are some alternatives:

### Other GLM Models
- `zhipuai/glm-4-plus` - More capable, higher cost
- `zhipuai/glm-4-air` - Lighter, faster
- `zhipuai/glm-4-flash` - Fastest, most cost-effective

### Other Providers via OpenRouter
- `anthropic/claude-3.5-sonnet` - Excellent reasoning
- `openai/gpt-4-turbo` - Strong general performance
- `google/gemini-pro-1.5` - Good multimodal support
- `meta-llama/llama-3.1-70b-instruct` - Open source, cost-effective

To switch models, just update the agent:
```bash
curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID \
  -H "Content-Type: application/json" \
  -d '{"llm": {"model": "NEW-MODEL-ID"}}' | jq
```

## Monitoring Usage

Check your OpenRouter usage and costs at:
https://openrouter.ai/activity

## Next Steps

- Explore memory features: `aria memories list`
- Try tool use: Enable tools in agent configuration
- Set up voice mode (Phase 8 - coming soon)
- Configure automation (Phase 11 - coming soon)

## Resources

- OpenRouter Docs: https://openrouter.ai/docs
- OpenRouter Models: https://openrouter.ai/models
- ARIA Specification: See `SPECIFICATION.md`
- Project Status: See `PROJECT_STATUS.md`
