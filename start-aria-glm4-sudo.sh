#!/bin/bash
# Quick start script for ARIA with GLM-4.7 via OpenRouter (DeepInfra)
# This version uses sudo for Docker commands

set -e

echo "🚀 Starting ARIA with GLM-4.7 via OpenRouter (DeepInfra)..."

# 1. Check .env file has OpenRouter key
if ! grep -q "OPENROUTER_API_KEY=sk-or" .env; then
    echo "⚠️  Please add your OpenRouter API key to .env file:"
    echo "   OPENROUTER_API_KEY=sk-or-v1-your-key-here"
    exit 1
fi

# 2. Start Docker services
echo "📦 Starting Docker services..."
sudo docker compose up -d

# 3. Wait for API to be ready
echo "⏳ Waiting for API to be ready..."
sleep 10
until curl -s http://localhost:8000/api/v1/health > /dev/null; do
    echo "   Still waiting..."
    sleep 3
done

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
if echo "$AGENT_RESPONSE" | grep -q "already exists\|duplicate"; then
    echo "ℹ️  GLM-4.7 agent already exists"
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
  -d "{\"agent_slug\": \"glm4\", \"title\": \"GLM-4.7 Chat\"}" | jq -r '.id')

echo "✅ Conversation ID: $CONV_ID"

echo ""
echo "🎉 ARIA is ready with GLM-4.7 (DeepInfra)!"
echo ""
echo "Quick commands:"
echo "  • Web UI: http://localhost:3000"
echo "  • Chat via API:"
echo "    curl -N -X POST http://localhost:8000/api/v1/conversations/$CONV_ID/messages \\"
echo "      -H 'Content-Type: application/json' \\"
echo "      -H 'Accept: text/event-stream' \\"
echo "      -d '{\"content\": \"Hello! Introduce yourself.\", \"stream\": true}'"
echo ""
echo "  • List agents: curl http://localhost:8000/api/v1/agents | jq"
echo "  • View logs: sudo docker compose logs -f api"
echo ""
echo "Model info:"
echo "  • Provider: DeepInfra (via OpenRouter)"
echo "  • Model: z-ai/glm-4.7"
echo "  • Context: ~128k tokens"
echo ""
echo "To make GLM-4.7 the default agent:"
echo "  curl -X PUT http://localhost:8000/api/v1/agents/$AGENT_ID -H 'Content-Type: application/json' -d '{\"is_default\": true}'"
echo ""
echo "💡 Tip: Add yourself to docker group to avoid using sudo:"
echo "  sudo usermod -aG docker $USER"
echo "  Then log out and back in (or run: newgrp docker)"
