#!/bin/bash
# Create an ARIA agent using OpenRouter with GLM-4.7 (DeepInfra)

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
      "long_term_results": 10,
      "categories_filter": null
    },
    "enabled_tools": [],
    "is_default": false
  }'
