import logging
import unittest

from log_record_loss_counter_utils import LossCounterHandler, LossSummary


class _FakeClock:
    """Deterministic clock for tests."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _RecordingHandler(logging.Handler):
    """Captures every record emitted, optionally raises on demand."""

    def __init__(self, level: int = logging.NOTSET) -> None:
        super().__init__(level=level)
        self.records: list[logging.LogRecord] = []
        self.emit_calls: int = 0
        self.flush_calls: int = 0
        self.close_calls: int = 0
        self._raise_until_call: int = -1

    def emit(self, record: logging.LogRecord) -> None:
        self.emit_calls += 1
        if self.emit_calls <= self._raise_until_call:
            raise RuntimeError("simulated downstream failure")
        self.records.append(record)

    def flush(self) -> None:
        self.flush_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def raise_on_first(self, n: int) -> None:
        self._raise_until_call = n


class TestLossSummary(unittest.TestCase):
    def test_repr_shows_fields(self) -> None:
        s = LossSummary(dropped=3, flushed_at=10.0)
        self.assertIn("3", repr(s))
        self.assertIn("10.0", repr(s))

    def test_eq_same_values(self) -> None:
        a = LossSummary(dropped=2, flushed_at=5.0)
        b = LossSummary(dropped=2, flushed_at=5.0)
        self.assertEqual(a, b)

    def test_eq_different_dropped(self) -> None:
        self.assertNotEqual(
            LossSummary(dropped=1, flushed_at=0.0),
            LossSummary(dropped=2, flushed_at=0.0),
        )

    def test_eq_different_time(self) -> None:
        self.assertNotEqual(
            LossSummary(dropped=1, flushed_at=0.0),
            LossSummary(dropped=1, flushed_at=1.0),
        )

    def test_eq_against_non_summary(self) -> None:
        self.assertNotEqual(LossSummary(dropped=0, flushed_at=0.0), 0)
        self.assertNotEqual(LossSummary(dropped=0, flushed_at=0.0), "x")
        self.assertNotEqual(LossSummary(dropped=0, flushed_at=0.0), None)


class TestLossCounterHandler(unittest.TestCase):
    def _make_handler(
        self,
        flush_interval: float = 60.0,
        clock: _FakeClock | None = None,
        wrapped: _RecordingHandler | None = None,
    ) -> tuple[LossCounterHandler, _RecordingHandler, _FakeClock]:
        wrapped = wrapped if wrapped is not None else _RecordingHandler()
        clock = clock if clock is not None else _FakeClock()
        h = LossCounterHandler(
            wrapped,
            flush_interval=flush_interval,
            clock=clock,
            fallback_logger_name="test_loss_counter",
        )
        return h, wrapped, clock

    def test_constructs_with_defaults(self) -> None:
        h, _, _ = self._make_handler()
        self.assertEqual(h.dropped_since_last_flush, 0)
        self.assertEqual(h.flush_interval, 60.0)
        self.assertIs(h.wrapped, h._wrapped)  # type: ignore[attr-defined]

    def test_emit_passes_through_when_healthy(self) -> None:
        h, wrapped, _ = self._make_handler()
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "hello", (), None
        )
        h.emit(record)
        self.assertEqual(wrapped.records, [record])
        self.assertEqual(h.dropped_since_last_flush, 0)

    def test_emit_counts_drop_on_error(self) -> None:
        h, wrapped, _ = self._make_handler()
        wrapped.raise_on_first(1)
        record = logging.LogRecord(
            "x", logging.INFO, __file__, 1, "hello", (), None
        )
        h.emit(record)  # raises -> counted
        self.assertEqual(h.dropped_since_last_flush, 1)
        self.assertEqual(wrapped.records, [])

        h.emit(record)  # now healthy -> passes through
        self.assertEqual(wrapped.records, [record])
        self.assertEqual(h.dropped_since_last_flush, 1)

    def test_maybe_flush_returns_none_before_interval(self) -> None:
        h, _, clock = self._make_handler(flush_interval=10.0)
        clock.advance(9.0)
        self.assertIsNone(h.maybe_flush())

    def test_maybe_flush_emits_when_interval_elapsed(self) -> None:
        h, wrapped, clock = self._make_handler(flush_interval=10.0)
        wrapped.raise_on_first(2)
        r1 = logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None)
        r2 = logging.LogRecord("x", logging.INFO, __file__, 1, "b", (), None)
        h.emit(r1)
        h.emit(r2)
        self.assertEqual(h.dropped_since_last_flush, 2)

        clock.advance(11.0)
        summary = h.maybe_flush()
        self.assertIsNotNone(summary)
        self.assertIsInstance(summary, LossSummary)
        assert summary is not None
        self.assertEqual(summary.dropped, 2)
        self.assertEqual(summary.flushed_at, clock.now)
        self.assertEqual(h.dropped_since_last_flush, 0)
        # One summary record flushed through the wrapped handler.
        self.assertEqual(len(wrapped.records), 1)
        self.assertIn("dropped 2", wrapped.records[0].getMessage())

    def test_maybe_flush_emits_nothing_when_zero_drops(self) -> None:
        h, wrapped, clock = self._make_handler(flush_interval=10.0)
        clock.advance(11.0)
        summary = h.maybe_flush()
        self.assertIsNotNone(summary)
        self.assertEqual(summary.dropped, 0)
        self.assertEqual(wrapped.records, [])

    def test_force_flush_bypasses_interval(self) -> None:
        h, wrapped, clock = self._make_handler(flush_interval=100.0)
        wrapped.raise_on_first(1)
        h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        summary = h.force_flush()
        self.assertEqual(summary.dropped, 1)
        self.assertEqual(summary.flushed_at, clock.now)
        self.assertEqual(len(wrapped.records), 1)

    def test_flush_resets_dropped_count(self) -> None:
        h, wrapped, _ = self._make_handler(flush_interval=10.0)
        wrapped.raise_on_first(1)
        h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        clock = _FakeClock(1000.0)
        h._clock = clock  # type: ignore[assignment]
        h._last_flush = 0.0  # type: ignore[assignment]
        summary = h.force_flush()
        self.assertEqual(summary.dropped, 1)
        self.assertEqual(h.dropped_since_last_flush, 0)
        # Another flush right after should report zero.
        self.assertEqual(h.force_flush().dropped, 0)

    def test_summary_emission_failure_does_not_loop_or_recount(self) -> None:
        # Wrapped handler raises on EVERY emit, including the summary.
        h, wrapped, _ = self._make_handler(flush_interval=1.0)
        wrapped.raise_on_first(1_000_000)
        for _ in range(3):
            h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        self.assertEqual(h.dropped_since_last_flush, 3)
        # Force flush: summary emission will also raise.
        summary = h.force_flush()
        self.assertEqual(summary.dropped, 3)
        # The dropped counter must be reset to zero even though the
        # summary itself failed to emit.
        self.assertEqual(h.dropped_since_last_flush, 0)

    def test_flush_calls_wrapped_flush(self) -> None:
        h, wrapped, clock = self._make_handler(flush_interval=10.0)
        clock.advance(11.0)
        h.flush()
        self.assertEqual(wrapped.flush_calls, 1)

    def test_flush_increments_on_wrapped_flush_failure(self) -> None:
        h, wrapped, clock = self._make_handler(flush_interval=10.0)
        clock.advance(11.0)
        original_flush = wrapped.flush
        wrapped.flush = lambda: (_ for _ in ()).throw(RuntimeError("nope"))  # type: ignore[assignment]
        try:
            h.flush()
        finally:
            wrapped.flush = original_flush  # type: ignore[assignment]
        self.assertEqual(h.dropped_since_last_flush, 1)

    def test_close_flushes_then_closes_wrapped(self) -> None:
        h, wrapped, _ = self._make_handler(flush_interval=1000.0)
        wrapped.raise_on_first(1)
        h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        h.close()
        self.assertEqual(wrapped.close_calls, 1)

    def test_emit_records_propagate_level_from_wrapped(self) -> None:
        wrapped = _RecordingHandler(level=logging.WARNING)
        h, _, _ = self._make_handler(wrapped=wrapped)
        self.assertEqual(h.level, logging.WARNING)

    def test_construction_rejects_negative_interval(self) -> None:
        with self.assertRaises(ValueError):
            LossCounterHandler(_RecordingHandler(), flush_interval=-1.0, clock=_FakeClock())

    def test_zero_flush_interval_flushes_every_emit(self) -> None:
        h, wrapped, _ = self._make_handler(flush_interval=0.0)
        wrapped.raise_on_first(1)
        h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        # With interval 0, the maybe_flush in emit fires immediately and
        # emits a summary record (one drop).
        self.assertEqual(len(wrapped.records), 1)
        self.assertIn("dropped 1", wrapped.records[0].getMessage())
        self.assertEqual(h.dropped_since_last_flush, 0)

    def test_summary_record_has_expected_fields(self) -> None:
        h, wrapped, _ = self._make_handler(flush_interval=1.0)
        wrapped.raise_on_first(1)
        h.emit(logging.LogRecord("x", logging.INFO, __file__, 1, "a", (), None))
        summary = h.force_flush()
        self.assertEqual(len(wrapped.records), 1)
        rec = wrapped.records[0]
        self.assertEqual(rec.levelno, logging.WARNING)
        self.assertEqual(rec.name, "log_record_loss_counter_utils.loss_summary")
        self.assertEqual(rec.args, (summary.dropped,))


if __name__ == "__main__":
    unittest.main()
