"""
ARIA - Lifecycle Hook System

Purpose: Plugin extension points at key orchestrator lifecycle stages.
Hooks are async callables registered by name. Multiple handlers per hook
are supported and executed in registration order.

Hook Points:
    pre_message      - Before processing a user message
    post_message     - After full response is generated
    pre_llm_call     - Before calling the LLM adapter
    post_llm_call    - After LLM response is complete
    pre_tool_call    - Before executing a tool
    post_tool_call   - After tool execution completes
    pre_memory_extract - Before background memory extraction
    post_memory_extract - After memory extraction completes
    on_error         - When an error occurs during processing
    on_fallback      - When the LLM falls back to another provider
"""

from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

# Hook handler signature: async (context: dict) -> dict | None
# Returning a dict merges updates into the context for downstream handlers.
HookHandler = Callable[[dict[str, Any]], Awaitable[dict[str, Any] | None]]

VALID_HOOKS = frozenset({
    "pre_message",
    "post_message",
    "pre_llm_call",
    "post_llm_call",
    "pre_tool_call",
    "post_tool_call",
    "pre_memory_extract",
    "post_memory_extract",
    "on_error",
    "on_fallback",
})


class HookRegistry:
    """Registry for lifecycle hook handlers."""

    def __init__(self):
        self._handlers: dict[str, list[tuple[str, HookHandler]]] = defaultdict(list)

    def register(self, hook_name: str, handler: HookHandler, *, label: str = "") -> None:
        """Register a handler for a lifecycle hook.

        Args:
            hook_name: One of the VALID_HOOKS.
            handler: Async callable receiving a context dict.
            label: Optional human-readable label for logging.
        """
        if hook_name not in VALID_HOOKS:
            raise ValueError(
                f"Unknown hook '{hook_name}'. Valid hooks: {', '.join(sorted(VALID_HOOKS))}"
            )
        self._handlers[hook_name].append((label or handler.__name__, handler))
        logger.info("Hook registered: %s -> %s", hook_name, label or handler.__name__)

    def unregister(self, hook_name: str, label: str) -> bool:
        """Unregister a handler by hook name and label."""
        handlers = self._handlers.get(hook_name, [])
        before = len(handlers)
        self._handlers[hook_name] = [(l, h) for l, h in handlers if l != label]
        removed = before - len(self._handlers[hook_name])
        if removed:
            logger.info("Hook unregistered: %s -> %s", hook_name, label)
        return removed > 0

    async def fire(self, hook_name: str, context: dict[str, Any]) -> dict[str, Any]:
        """Fire all handlers for a hook, passing and merging context.

        Handlers are called in registration order. Each handler receives the
        accumulated context and can return updates to merge in.

        If a handler raises, the error is logged and execution continues
        with the remaining handlers (fail-open).
        """
        handlers = self._handlers.get(hook_name, [])
        if not handlers:
            return context

        for label, handler in handlers:
            try:
                updates = await handler(context)
                if updates and isinstance(updates, dict):
                    context.update(updates)
            except Exception:
                logger.error(
                    "Hook handler '%s' for '%s' raised an exception",
                    label, hook_name, exc_info=True,
                )

        return context

    def list_hooks(self) -> dict[str, list[str]]:
        """List all registered hooks and their handler labels."""
        return {
            hook: [label for label, _ in handlers]
            for hook, handlers in self._handlers.items()
            if handlers
        }

    def clear(self, hook_name: str | None = None) -> int:
        """Clear handlers for a specific hook or all hooks."""
        if hook_name:
            count = len(self._handlers.pop(hook_name, []))
            return count
        count = sum(len(h) for h in self._handlers.values())
        self._handlers.clear()
        return count


# Module-level singleton
hook_registry = HookRegistry()
