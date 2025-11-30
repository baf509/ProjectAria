# ARIA CLI

Command-line client for ARIA - Local AI Agent Platform

## Installation

```bash
pip install -r requirements.txt
pip install -e .
```

## Usage

```bash
# Health check
aria health

# Interactive chat
aria chat

# One-shot message
aria chat "Hello, ARIA!"

# List conversations
aria conversations list

# Continue conversation
aria chat -c <conversation-id> "Continue"

# List agents
aria agents list

# Memory commands
aria memories list
aria memories search "query"
aria memories add "fact" --type fact
```

## Requirements

- Python 3.12+
- ARIA API running (http://localhost:8000)

## Documentation

See main repository README and GETTING_STARTED.md for full documentation.
