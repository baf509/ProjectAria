"""
ARIA - llama.cpp Adapter

Phase: 1
Purpose: llama.cpp server adapter (OpenAI-compatible API)

Related Spec Sections:
- Section 6: LLM Adapter Interface
"""

from aria.config import settings
from aria.llm.openai import OpenAIAdapter

try:
    from openai import AsyncOpenAI
    OPENAI_AVAILABLE = True
except ImportError:
    OPENAI_AVAILABLE = False


class LlamaCppAdapter(OpenAIAdapter):
    """
    Adapter for llama.cpp server.

    llama.cpp exposes an OpenAI-compatible API, so this is a thin
    wrapper around OpenAIAdapter that points to the local server.
    """

    def __init__(
        self,
        base_url: str,
        model: str = "default",
        api_key: str = "",
        timeout_seconds: float | None = None,
    ):
        if not OPENAI_AVAILABLE:
            raise ImportError(
                "openai package not installed. "
                "Install with: pip install openai"
            )

        self.api_key = api_key or "no-key"
        self.model = model
        # Explicit timeout: the SDK default (600s) lets a busy/half-open local
        # server hang a caller for ~10min per try. retry_async can't help — a
        # hang never raises. See settings.llamacpp_timeout_seconds.
        #
        # timeout_seconds overrides it for backends that are legitimately slower
        # to first byte — notably "ridge", where the proxy WoL-wakes a sleeping
        # box and holds the request through a ~90s cold path.
        self.client = AsyncOpenAI(
            base_url=base_url,
            api_key=self.api_key,
            # Every adapter created here belongs to ARIA's own orchestration,
            # stewardship, review, or maintenance loops. Pi and Hermes use
            # their own clients and caller labels. Mark these calls as
            # background so the gateway can keep the human front door
            # responsive when work stacks up on a one-slot deployment.
            default_headers={"X-Aria-Caller": "aria-background"},
            timeout=float(
                timeout_seconds
                if timeout_seconds is not None
                else settings.llamacpp_timeout_seconds
            ),
        )

    @property
    def name(self) -> str:
        return "llamacpp"
