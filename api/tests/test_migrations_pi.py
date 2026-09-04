from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from aria.db.migrations import _reconcile_pi_coding_profiles


@pytest.mark.asyncio
async def test_reconcile_pi_coding_profiles_pins_hybrid_without_touching_prompts():
    db = MagicMock()
    db.agents.update_many = AsyncMock(return_value=SimpleNamespace(modified_count=2))

    await _reconcile_pi_coding_profiles(db)

    query, update = db.agents.update_many.await_args.args
    assert set(query["slug"]["$in"]) == {"pi-coding", "pi-coding-ridge"}
    assert update["$set"]["llm.backend"] == "aria"
    assert update["$set"]["llm.model"] == "Qwen3.8-Flash-Next-Hybrid-R9700-Halo"
    assert update["$set"]["llm.max_tokens"] == 32768
    assert update["$set"]["llm.max_context_tokens"] == 262144
    assert "system_prompt" not in update["$set"]
