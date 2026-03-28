"""Tests for aria.memory.long_term — RRF fusion and binary embedding utils."""

from datetime import datetime, timezone

import pytest

from aria.memory.long_term import (
    LongTermMemory,
    Memory,
    binary_to_embedding,
    embedding_to_binary,
)


# ---------------------------------------------------------------------------
# Binary embedding encoding/decoding
# ---------------------------------------------------------------------------

class TestEmbeddingBinary:
    def test_roundtrip(self):
        original = [0.1, 0.2, 0.3, 0.4, 0.5]
        binary = embedding_to_binary(original)
        restored = binary_to_embedding(binary)
        assert len(restored) == len(original)
        for a, b in zip(original, restored):
            assert abs(a - b) < 1e-6

    def test_empty_embedding(self):
        binary = embedding_to_binary([])
        restored = binary_to_embedding(binary)
        assert restored == []

    def test_single_value(self):
        binary = embedding_to_binary([42.0])
        restored = binary_to_embedding(binary)
        assert len(restored) == 1
        assert abs(restored[0] - 42.0) < 1e-6

    def test_1024_dimensions(self):
        """Verify we can handle ARIA's actual embedding dimension."""
        original = [float(i) / 1024 for i in range(1024)]
        binary = embedding_to_binary(original)
        restored = binary_to_embedding(binary)
        assert len(restored) == 1024
        for a, b in zip(original, restored):
            assert abs(a - b) < 1e-5


# ---------------------------------------------------------------------------
# Memory.from_doc
# ---------------------------------------------------------------------------

class TestMemoryFromDoc:
    def test_basic_document(self):
        now = datetime.now(timezone.utc)
        doc = {
            "_id": "abc123",
            "content": "Python is great",
            "content_type": "fact",
            "categories": ["programming"],
            "importance": 0.8,
            "created_at": now,
            "source": {"type": "conversation"},
            "confidence": 0.9,
            "verified": True,
        }
        memory = Memory.from_doc(doc)
        assert memory.id == "abc123"
        assert memory.content == "Python is great"
        assert memory.content_type == "fact"
        assert memory.categories == ["programming"]
        assert memory.importance == 0.8
        assert memory.confidence == 0.9
        assert memory.verified is True

    def test_defaults_for_missing_fields(self):
        doc = {
            "_id": "x",
            "content": "test",
            "content_type": "fact",
            "created_at": datetime.now(timezone.utc),
        }
        memory = Memory.from_doc(doc)
        assert memory.categories == []
        assert memory.importance == 0.5
        assert memory.source == {}
        assert memory.confidence is None
        assert memory.verified is False


# ---------------------------------------------------------------------------
# RRF Fusion
# ---------------------------------------------------------------------------

class TestRRFFusion:
    def _make_memory(self, id: str, content: str = "test") -> Memory:
        return Memory(
            id=id,
            content=content,
            content_type="fact",
            categories=[],
            importance=0.5,
            created_at=datetime.now(timezone.utc),
            source={},
        )

    def test_basic_fusion(self, mock_db):
        ltm = LongTermMemory(mock_db)
        m1 = self._make_memory("m1")
        m2 = self._make_memory("m2")
        m3 = self._make_memory("m3")

        vector_results = [(m1, 0.9), (m2, 0.7)]
        lexical_results = [(m2, 5.0), (m3, 3.0)]

        fused = ltm._rrf_fusion(vector_results, lexical_results, k=60)

        # m2 appears in both lists so should rank highest
        assert fused[0][0].id == "m2"
        assert len(fused) == 3

    def test_empty_inputs(self, mock_db):
        ltm = LongTermMemory(mock_db)
        fused = ltm._rrf_fusion([], [], k=60)
        assert fused == []

    def test_single_list(self, mock_db):
        ltm = LongTermMemory(mock_db)
        m1 = self._make_memory("m1")
        fused = ltm._rrf_fusion([(m1, 0.9)], [], k=60)
        assert len(fused) == 1
        assert fused[0][0].id == "m1"

    def test_no_overlap(self, mock_db):
        ltm = LongTermMemory(mock_db)
        m1 = self._make_memory("m1")
        m2 = self._make_memory("m2")

        fused = ltm._rrf_fusion([(m1, 0.9)], [(m2, 5.0)], k=60)
        assert len(fused) == 2
        # Both should have equal RRF scores (both rank 1 in their list)
        ids = {m.id for m, _ in fused}
        assert ids == {"m1", "m2"}

    def test_rank_ordering_matters(self, mock_db):
        ltm = LongTermMemory(mock_db)
        m1 = self._make_memory("m1")
        m2 = self._make_memory("m2")
        m3 = self._make_memory("m3")

        # m1 at rank 0 in both lists should beat m2 at rank 1 in vector only
        vector_results = [(m1, 0.9), (m2, 0.7), (m3, 0.5)]
        lexical_results = [(m1, 5.0)]

        fused = ltm._rrf_fusion(vector_results, lexical_results, k=60)
        assert fused[0][0].id == "m1"
