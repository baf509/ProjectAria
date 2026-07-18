"""Tests for Shared Services: S5 native vectors, S2 diff, S3 ownership."""
import struct

from bson import Binary
from bson.binary import VECTOR_SUBTYPE

from aria.memory.long_term import embedding_to_binary, binary_to_embedding
from aria.shared.scan import _diff
from aria.shared.ownership import merge_owned


class TestS5NativeVectors:
    def test_encode_is_subtype_9(self):
        b = embedding_to_binary([0.1, 0.2, 0.3])
        assert b.subtype == VECTOR_SUBTYPE  # 9

    def test_roundtrip(self):
        vals = [0.1, -0.2, 0.3, 1.5, 0.0]
        out = binary_to_embedding(embedding_to_binary(vals))
        assert all(abs(a - b) < 1e-6 for a, b in zip(vals, out))
        assert len(out) == len(vals)

    def test_decodes_legacy_subtype_0(self):
        # pre-S5 docs: raw little-endian float32, subtype 0
        legacy = Binary(struct.pack("3f", 0.4, 0.5, 0.6), subtype=0)
        out = binary_to_embedding(legacy)
        assert [round(x, 3) for x in out] == [0.4, 0.5, 0.6]


class TestS2Diff:
    def test_added_and_removed(self):
        prev = {"services": ["a.service", "b.service"], "containers": [], "ports": []}
        cur = {"services": ["b.service", "c.service"], "containers": [], "ports": []}
        d = _diff(prev, cur)
        assert d["services"]["added"] == ["c.service"]
        assert d["services"]["removed"] == ["a.service"]

    def test_no_change_is_empty(self):
        snap = {"services": ["a.service"], "containers": ["x"], "ports": ["8200"]}
        assert _diff(snap, snap) == {}

    def test_first_run_all_added(self):
        cur = {"services": ["a.service"], "containers": [], "ports": []}
        assert _diff(None, cur)["services"]["added"] == ["a.service"]


class TestS3Ownership:
    def test_worker_writes_only_owned_fields(self):
        existing = {"name": "x", "summary": "human note"}
        observed = {"name": "x", "port": 8200, "summary": "robot note"}
        upd, conflicts = merge_owned(existing, observed, worker_fields={"name", "port"})
        assert "port" in upd
        assert "summary" not in upd  # human-owned, never written by the worker

    def test_flags_human_conflict(self):
        existing = {"summary": "human note", "source": {"summary": {"actor": "human"}}}
        observed = {"summary": "robot note"}
        _, conflicts = merge_owned(existing, observed, worker_fields={"name"})
        assert "summary" in conflicts

    def test_provenance_stamped(self):
        upd, _ = merge_owned({}, {"port": 8200}, worker_fields={"port"}, actor="scan")
        assert upd["source"]["port"]["actor"] == "scan"
        assert "last_verified_at" in upd
