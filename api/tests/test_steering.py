"""
Tests for aria.core.steering.SteeringQueue.
"""

import pytest

from aria.core.steering import SteeringQueue


class TestSteeringQueue:

    def test_enqueue_and_drain(self):
        sq = SteeringQueue()
        assert sq.enqueue("conv-1", "do X") is True
        assert sq.enqueue("conv-1", "do Y") is True

        messages = sq.drain("conv-1")
        assert len(messages) == 2
        assert messages[0].content == "do X"
        assert messages[1].content == "do Y"

    def test_drain_empty(self):
        sq = SteeringQueue()
        assert sq.drain("conv-nonexistent") == []

    def test_drain_orders_interrupts_first(self):
        sq = SteeringQueue()
        sq.enqueue("conv-1", "normal msg", priority="normal")
        sq.enqueue("conv-1", "urgent msg", priority="interrupt")
        sq.enqueue("conv-1", "another normal", priority="normal")

        messages = sq.drain("conv-1")
        assert messages[0].content == "urgent msg"
        assert messages[0].priority == "interrupt"
        # Remaining are normal priority
        assert all(m.priority == "normal" for m in messages[1:])

    def test_enqueue_full_queue(self):
        sq = SteeringQueue()
        # Fill the queue (maxsize=10)
        for i in range(10):
            assert sq.enqueue("conv-1", f"msg {i}") is True

        # 11th should fail
        assert sq.enqueue("conv-1", "overflow") is False

    def test_has_interrupt_true(self):
        sq = SteeringQueue()
        sq.enqueue("conv-1", "urgent", priority="interrupt")
        assert sq.has_interrupt("conv-1") is True

    def test_has_interrupt_false(self):
        sq = SteeringQueue()
        sq.enqueue("conv-1", "normal msg", priority="normal")
        assert sq.has_interrupt("conv-1") is False

    def test_has_interrupt_preserves_queue(self):
        sq = SteeringQueue()
        sq.enqueue("conv-1", "urgent", priority="interrupt")
        sq.enqueue("conv-1", "normal", priority="normal")

        # Peek should not consume
        assert sq.has_interrupt("conv-1") is True

        # All messages should still be there
        messages = sq.drain("conv-1")
        assert len(messages) == 2

    def test_clear(self):
        sq = SteeringQueue()
        sq.enqueue("conv-1", "msg 1")
        sq.enqueue("conv-1", "msg 2")
        sq.enqueue("conv-1", "msg 3")

        count = sq.clear("conv-1")
        assert count == 3

        # Queue should be empty now
        assert sq.drain("conv-1") == []

    def test_clear_nonexistent(self):
        sq = SteeringQueue()
        assert sq.clear("conv-nonexistent") == 0
