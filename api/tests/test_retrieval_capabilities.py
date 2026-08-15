"""
Tests for the retrieval capability switches (memory/capabilities.py), the
degraded search paths in memory/long_term.py, and the embedding backfill worker.

Each test corresponds to a way this could quietly fail to deliver on the
promise "turn mongot/embeddings off without stopping the service, turn them
back on and everything missed gets picked up":

  - a disabled capability that still dials the service (the whole point is that
    it costs nothing while off);
  - a disabled capability that raises instead of degrading, taking recall from
    "worse" to "500";
  - a write that loses its embedding AND its pending flag, so nothing ever
    comes back for it — an invisible permanent hole in recall;
  - a backfill that runs while the capability is off, defeating the switch;
  - a *good* embedding thrown away because the OTHER capability was off.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from aria.memory.capabilities import (
    EmbeddingsDisabled,
    RetrievalCapabilities,
    retrieval_capabilities,
)
from aria.memory.long_term import LongTermMemory, SearchBranchUnavailable


@pytest.fixture(autouse=True)
def reset_capabilities():
    """The switches are a process-wide singleton; never leak state between tests."""
    yield
    retrieval_capabilities._embeddings.enabled = True
    retrieval_capabilities._search.enabled = True
    retrieval_capabilities._db = None
    retrieval_capabilities._on_embeddings_enabled = None


# ---------------------------------------------------------------------------
# The switches themselves
# ---------------------------------------------------------------------------

class TestRetrievalCapabilities:
    def test_defaults_are_on(self):
        caps = RetrievalCapabilities()
        assert caps.embeddings_enabled is True
        assert caps.search_enabled is True
        assert caps.retrieval_mode() == "hybrid"

    def test_retrieval_mode_matrix(self):
        caps = RetrievalCapabilities()
        caps._embeddings.enabled = False
        assert caps.retrieval_mode() == "lexical"
        assert caps.vector_search_enabled is False

        caps._embeddings.enabled = True
        caps._search.enabled = False
        # No mongot -> no $vectorSearch either, however good the embeddings are.
        assert caps.retrieval_mode() == "fallback"
        assert caps.vector_search_enabled is False

    @pytest.mark.asyncio
    async def test_toggle_records_reason_and_persists(self):
        caps = RetrievalCapabilities()
        db = MagicMock()
        db.__getitem__.return_value.update_one = AsyncMock()
        caps.set_db(db)

        await caps.set_search(False, reason="freeing RAM", changed_by="ben")

        assert caps.search_enabled is False
        status = caps.status()["search"]
        assert status["reason"] == "freeing RAM"
        assert status["changed_by"] == "ben"
        assert status["changed_at"] is not None
        db.__getitem__.return_value.update_one.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_load_state_keeps_a_disabled_capability_disabled(self):
        """A restart must not silently re-enable what an operator switched off."""
        caps = RetrievalCapabilities()
        db = MagicMock()
        db.__getitem__.return_value.find_one = AsyncMock(
            return_value={
                "_id": "retrieval",
                "embeddings": {"enabled": False, "reason": "container stopped"},
                "search": {"enabled": True},
            }
        )
        await caps.load_state(db)

        assert caps.embeddings_enabled is False
        assert caps.search_enabled is True
        assert caps.status()["embeddings"]["reason"] == "container stopped"

    @pytest.mark.asyncio
    async def test_reenabling_embeddings_kicks_the_backfill(self):
        caps = RetrievalCapabilities()
        caps._embeddings.enabled = False
        kicked = []
        caps.set_backfill_trigger(lambda: kicked.append(True))

        await caps.set_embeddings(True, reason="back up")

        assert kicked == [True], "re-enabling must drain the backlog without a second call"

    @pytest.mark.asyncio
    async def test_disabling_does_not_kick(self):
        caps = RetrievalCapabilities()
        kicked = []
        caps.set_backfill_trigger(lambda: kicked.append(True))
        await caps.set_embeddings(False, reason="off")
        assert kicked == []


# ---------------------------------------------------------------------------
# Embedding service gate
# ---------------------------------------------------------------------------

class TestEmbeddingServiceGate:
    @pytest.mark.asyncio
    async def test_embed_raises_without_calling_the_service(self):
        from aria.memory.embeddings import EmbeddingService

        service = EmbeddingService()
        service.primary.embed = AsyncMock(side_effect=AssertionError("must not be called"))
        retrieval_capabilities._embeddings.enabled = False

        with pytest.raises(EmbeddingsDisabled):
            await service.embed("anything")
        service.primary.embed.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embed_or_none_degrades_quietly(self):
        from aria.memory.embeddings import EmbeddingService

        service = EmbeddingService()
        service.primary.embed = AsyncMock(side_effect=AssertionError("must not be called"))
        retrieval_capabilities._embeddings.enabled = False

        assert await service.embed_or_none("anything") is None


# ---------------------------------------------------------------------------
# Degraded search paths
# ---------------------------------------------------------------------------

def _memory_doc(content: str, **extra) -> dict:
    doc = {
        "_id": content,
        "content": content,
        "content_type": "fact",
        "categories": [],
        "importance": 0.5,
        "created_at": datetime.now(timezone.utc),
        "source": {"type": "test"},
        "status": "active",
    }
    doc.update(extra)
    return doc


def _db_with_memories(docs: list[dict]) -> MagicMock:
    db = MagicMock()
    cursor = MagicMock()
    cursor.sort = MagicMock(return_value=cursor)
    cursor.limit = MagicMock(return_value=cursor)
    cursor.to_list = AsyncMock(return_value=docs)
    db.memories.find = MagicMock(return_value=cursor)
    db.memories.aggregate = MagicMock(
        side_effect=AssertionError("mongot must not be queried while search is off")
    )
    db.memories.find_one = AsyncMock(return_value=None)
    db.memories.insert_one = AsyncMock(return_value=MagicMock(inserted_id="new-id"))
    db.memories.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
    return db


class TestSearchDegradation:
    @pytest.mark.asyncio
    async def test_search_off_uses_fallback_and_never_embeds(self):
        retrieval_capabilities._search.enabled = False
        db = _db_with_memories([
            _memory_doc("the R9700 is the discrete GPU"),
            _memory_doc("unrelated note about coffee"),
        ])
        ltm = LongTermMemory(db)

        with patch(
            "aria.memory.long_term.embedding_service.embed",
            AsyncMock(side_effect=AssertionError("must not embed with nowhere to send it")),
        ):
            results = await ltm.search("R9700", limit=5)

        # Ranked by token overlap: the matching memory comes first.
        assert results[0].content == "the R9700 is the discrete GPU"

    @pytest.mark.asyncio
    async def test_fallback_ignores_stopwords(self):
        """A query of stopwords must not match every memory in the collection."""
        retrieval_capabilities._search.enabled = False
        db = _db_with_memories([_memory_doc("the quick brown fox")])
        ltm = LongTermMemory(db)

        await ltm.search("what is the", limit=5)

        # With every token filtered out there is no $or clause — a plain
        # recency-ordered scan, not a regex over noise words.
        query = db.memories.find.call_args[0][0]
        assert "$or" not in query

    @pytest.mark.asyncio
    async def test_embeddings_off_runs_lexical_only(self):
        retrieval_capabilities._embeddings.enabled = False
        db = MagicMock()
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[
            {**_memory_doc("bm25 result"), "score": 1.0},
        ])
        db.memories.aggregate = MagicMock(return_value=agg_cursor)
        ltm = LongTermMemory(db)

        with patch(
            "aria.memory.long_term.embedding_service.embed",
            AsyncMock(side_effect=AssertionError("must not embed while embeddings are off")),
        ):
            results = await ltm.search("anything", limit=5)

        assert [m.content for m in results] == ["bm25 result"]
        # Exactly one aggregate: the $search branch. No $vectorSearch.
        assert db.memories.aggregate.call_count == 1
        stages = db.memories.aggregate.call_args[0][0]
        assert "$search" in stages[0]

    @pytest.mark.asyncio
    async def test_dead_mongot_degrades_to_the_fallback_scan(self):
        """Both branches unavailable is NOT the same as 'nothing matched'."""
        db = _db_with_memories([_memory_doc("stored while mongot was dead")])
        ltm = LongTermMemory(db)

        with patch(
            "aria.memory.long_term.embedding_service.embed",
            AsyncMock(return_value=[0.1] * 1024),
        ), patch.object(
            LongTermMemory, "_vector_search", AsyncMock(side_effect=SearchBranchUnavailable("down"))
        ), patch.object(
            LongTermMemory, "_lexical_search", AsyncMock(side_effect=SearchBranchUnavailable("down"))
        ):
            results = await ltm.search("mongot", limit=5)

        assert [m.content for m in results] == ["stored while mongot was dead"]

    @pytest.mark.asyncio
    async def test_empty_result_from_healthy_mongot_is_respected(self):
        """A genuine miss must NOT silently fall through to the scan."""
        db = MagicMock()
        agg_cursor = MagicMock()
        agg_cursor.to_list = AsyncMock(return_value=[])
        db.memories.aggregate = MagicMock(return_value=agg_cursor)
        db.memories.find = MagicMock(side_effect=AssertionError("must not scan"))
        ltm = LongTermMemory(db)

        with patch(
            "aria.memory.long_term.embedding_service.embed",
            AsyncMock(return_value=[0.1] * 1024),
        ):
            assert await ltm.search("nothing matches this", limit=5) == []


# ---------------------------------------------------------------------------
# Writes while degraded
# ---------------------------------------------------------------------------

class TestDegradedWrites:
    @pytest.mark.asyncio
    async def test_memory_is_stored_pending_when_embeddings_are_off(self):
        retrieval_capabilities._embeddings.enabled = False
        db = _db_with_memories([])
        ltm = LongTermMemory(db)

        memory_id = await ltm.create_memory("a fact worth keeping", "fact")

        assert memory_id == "new-id"
        doc = db.memories.insert_one.call_args[0][0]
        assert doc["embedding"] is None
        assert doc["embedding_pending"] is True, "the flag IS the backfill queue"
        assert doc["content"] == "a fact worth keeping"

    @pytest.mark.asyncio
    async def test_exact_duplicates_are_still_absorbed_while_degraded(self):
        retrieval_capabilities._embeddings.enabled = False
        db = _db_with_memories([])
        db.memories.find_one = AsyncMock(return_value={"_id": "existing-id"})
        ltm = LongTermMemory(db)

        memory_id = await ltm.create_memory("repeated machine-scan line", "fact")

        assert memory_id == "existing-id"
        db.memories.insert_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_good_embedding_survives_mongot_being_off(self):
        """Only vector *dedup* needs mongot — the vector itself must be stored."""
        retrieval_capabilities._search.enabled = False
        db = _db_with_memories([])
        ltm = LongTermMemory(db)

        with patch(
            "aria.memory.long_term.embedding_service.embed_or_none",
            AsyncMock(return_value=[0.25] * 1024),
        ):
            await ltm.create_memory("embedded even with mongot down", "fact")

        doc = db.memories.insert_one.call_args[0][0]
        assert doc["embedding"] is not None
        assert doc["embedding_pending"] is False


# ---------------------------------------------------------------------------
# Backfill
# ---------------------------------------------------------------------------

class TestEmbeddingBackfill:
    def _worker(self, docs: list[dict]):
        from aria.memory.backfill import EmbeddingBackfillWorker

        db = MagicMock()
        cursor = MagicMock()
        cursor.limit = MagicMock(return_value=cursor)
        cursor.to_list = AsyncMock(return_value=docs)
        db.memories.find = MagicMock(return_value=cursor)
        db.memories.update_one = AsyncMock(return_value=MagicMock(modified_count=1))
        db.memories.count_documents = AsyncMock(return_value=0)

        entities = MagicMock()
        ent_cursor = MagicMock()
        ent_cursor.limit = MagicMock(return_value=ent_cursor)
        ent_cursor.to_list = AsyncMock(return_value=[])
        entities.find = MagicMock(return_value=ent_cursor)
        entities.count_documents = AsyncMock(return_value=0)
        db.__getitem__ = MagicMock(return_value=entities)

        return EmbeddingBackfillWorker(db), db

    @pytest.mark.asyncio
    async def test_does_not_run_while_the_capability_is_off(self):
        retrieval_capabilities._embeddings.enabled = False
        worker, db = self._worker([_memory_doc("pending", embedding_pending=True)])

        result = await worker.run_once()

        assert result["skipped"] == "embeddings disabled"
        db.memories.update_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_embeds_the_pending_backlog(self):
        worker, db = self._worker([
            _memory_doc("missed one", embedding_pending=True),
            _memory_doc("missed two", embedding_pending=True),
        ])

        with patch(
            "aria.memory.backfill.embedding_service.embed_or_none",
            AsyncMock(return_value=[0.5] * 1024),
        ):
            result = await worker.run_once()

        assert result["memories_embedded"] == 2
        assert result["memories_failed"] == 0
        updates = [c[0][1]["$set"] for c in db.memories.update_one.call_args_list]
        assert all(u["embedding_pending"] is False for u in updates)
        assert all(u["embedding"] is not None for u in updates)

    @pytest.mark.asyncio
    async def test_failures_keep_their_flag_for_the_next_pass(self):
        worker, db = self._worker([_memory_doc("still missed", embedding_pending=True)])

        with patch(
            "aria.memory.backfill.embedding_service.embed_or_none",
            AsyncMock(return_value=None),
        ):
            result = await worker.run_once()

        assert result["memories_embedded"] == 0
        assert result["memories_failed"] == 1
        db.memories.update_one.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_content_is_retired_rather_than_looping_forever(self):
        worker, db = self._worker([_memory_doc("", embedding_pending=True)])

        with patch(
            "aria.memory.backfill.embedding_service.embed_or_none",
            AsyncMock(side_effect=AssertionError("nothing to embed")),
        ):
            result = await worker.run_once()

        assert result["memories_embedded"] == 1
        update = db.memories.update_one.call_args[0][1]["$set"]
        assert update["embedding_pending"] is False
        assert update["embedding_skipped"] == "empty content"
