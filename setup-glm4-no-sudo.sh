#!/bin/bash
# GLM-4.7 setup script (no sudo required)

set -e

echo "🚀 Starting ARIA with GLM-4.7 via OpenRouter (DeepInfra)..."

# 1. Check .env file has OpenRouter key
if ! grep -q "OPENROUTER_API_KEY=sk-or" .env; then
    echo "⚠️  Please add your OpenRouter API key to .env file:"
    echo "   OPENROUTER_API_KEY=sk-or-v1-your-key-here"
    exit 1
fi
echo "✅ OpenRouter API key configured"

# 2. Check if Docker services are running, if not start them
echo "📦 Checking Docker services..."
if ! docker ps --filter "name=aria-api" --format "{{.Names}}" | grep -q "aria-api"; then
    echo "   Starting Docker services..."
    docker compose up -d
else
    echo "✅ Docker services already running"
fi

# 3. Wait for API to be ready
echo "⏳ Waiting for API to be ready..."
sleep 5
MAX_ATTEMPTS=30
ATTEMPT=0
until curl -s http://localhost:8000/api/v1/health > /dev/null 2>&1; do
    ATTEMPT=$((ATTEMPT + 1))
    if [ $ATTEMPT -ge $MAX_ATTEMPTS ]; then
        echo "❌ API did not become ready in time"
        echo "Check logs with: docker compose logs -f api"
        exit 1
    fi
    echo "   Attempt $ATTEMPT/$MAX_ATTEMPTS..."
    sleep 2
done
echo "✅ API is ready"

# 4. Check OpenRouter is configured
echo "🔍 Checking OpenRouter configuration..."
OPENROUTER_STATUS=$(curl -s http://localhost:8000/api/v1/health/llm | jq -r '.[] | select(.backend=="openrouter") | .available')
if [ "$OPENROUTER_STATUS" != "true" ]; then
    echo "❌ OpenRouter not configured properly"
    curl -s http://localhost:8000/api/v1/health/llm | jq '.[] | select(.backend=="openrouter")'
    exit 1
fi
echo "✅ OpenRouter is ready"

# 5. Create GLM-4.7 agent
echo "🤖 Creating GLM-4.7 agent (DeepInfra)..."
AGENT_RESPONSE=$(curl -s -X POST http://localhost:8000/api/v1/agents \
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
      "long_term_results": 10,
      "categories_filter": null
    },
    "enabled_tools": [],
    "is_default": false
  }' 2>&1)

# Check if agent already exists
if echo "$AGENT_RESPONSE" | grep -qi "already exists\|duplicate"; then
    echo "ℹ️  GLM-4.7 agent already exists, updating..."
    AGENT_ID=$(curl -s http://localhost:8000/api/v1/agents | jq -r '.[] | select(.slug=="glm4") | .id')
else
    AGENT_ID=$(echo "$AGENT_RESPONSE" | jq -r '.id')
fi

if [ -z "$AGENT_ID" ] || [ "$AGENT_ID" == "null" ]; then
    echo "❌ Failed to create/find agent"
    echo "$AGENT_RESPONSE" | jq
    exit 1
fi

echo "✅ Agent ID: $AGENT_ID"

# 6. Create a conversation with GLM-4.7 agent
echo "💬 Creating conversation..."
CONV_ID=$(curl -s -X POST http://localhost:8000/api/v1/conversations \
  -H "Content-Type: application/json" \
  -d "{\"agent_slug\": \"glm4\", \"title\": \"GLM-4.7 Test Chat\"}" | jq -r '.id')

if [ -z "$CONV_ID" ] || [ "$CONV_ID" == "null" ]; then
    echo "❌ Failed to create conversation"
    exit 1
fi

echo "✅ Conversation ID: $CONV_ID"

# 7. Send a test message
echo ""
echo "📨 Sending test message..."
echo ""

curl -N -X POST http://localhost:8000/api/v1/conversations/$CONV_ID/messages \
  -H "Content-Type: application/json" \
  -H "Accept: text/event-stream" \
  -d '{"content": "Hello! Please introduce yourself in 2-3 sentences.", "stream": true}' 2>/dev/null | \
  grep "data:" | sed 's/^data: //' | jq -r 'select(.content != null) | .content' | tr -d '\n'

echo ""
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "🎉 ARIA is ready with GLM-4.7 (DeepInfra)!"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "Quick commands:"
echo "  • Web UI: http://localhost:3000"
echo "  • Conversation ID: $CONV_ID"
echo ""
echo "Send another message:"
echo "  curl -N -X POST http://localhost:8000/api/v1/conversations/$CONV_ID/messages \\"
echo "    -H 'Content-Type: application/json' \\"
echo "    -H 'Accept: text/event-stream' \\"
echo "    -d '{\"content\": \"Your message here\", \"stream\": true}' | \\"
echo "    grep 'data:' | sed 's/^data: //' | jq -r 'select(.content != null) | .content' | tr -d '\\n'"
echo ""
echo "View all agents:"
echo "  curl -s http://localhost:8000/api/v1/agents | jq"
echo ""
echo "Model info:"
echo "  • Provider: DeepInfra (via OpenRouter)"
echo "  • Model: z-ai/glm-4.7"
echo "  • Agent ID: $AGENT_ID"
echo ""
