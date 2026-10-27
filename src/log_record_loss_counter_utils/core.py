from __future__ import annotations

import logging
from typing import Callable, List, Optional


class LossSummary:
    """A snapshot of how many records a handler has dropped.

    Kept as a plain value object (rather than just exposing an integer) so
    that callers can distinguish "zero losses since the last flush" from
    "no flush has happened yet". ``flushed_at`` is whatever the configured
    clock returned at flush time, which makes it stubbable in tests.
    """

    __slots__ = ("dropped", "flushed_at")

    def __init__(self, dropped: int, flushed_at: float) -> None:
        self.dropped = dropped
        self.flushed_at = flushed_at

    def __repr__(self) -> str:  # pragma: no cover - trivial
        return "LossSummary(dropped={!r}, flushed_at={!r})".format(
            self.dropped, self.flushed_at
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LossSummary):
            return NotImplemented
        return self.dropped == other.dropped and self.flushed_at == other.flushed_at


class LossCounterHandler(logging.Handler):
    """Wraps another handler, counting records dropped due to errors.

    Records are "dropped" when the wrapped handler raises while emitting
    (most commonly because the downstream sink — a file, a socket — is
    unavailable). Instead of surfacing every error, this handler swallows
    them, increments a counter, and periodically flushes a single summary
    record describing the accumulated loss.

    The flush cadence is driven by the ``clock`` callable so tests can
    advance time deterministically. We do NOT call ``time.time()`` directly
    anywhere — that would couple the tests to wall-clock time.

    Design decisions, stated plainly:

    * **Swallow, don't propagate.** ``logging.Handler.emit`` already catches
      exceptions by default, but its default behaviour both swallows AND
      prints a traceback to ``sys.stderr``. We override to swallow quietly
      and convert the loss into a number. The trade-off: the operator loses
      per-record error detail in exchange for not being spammed when a
      sink fails.
    * **Summary records go through the wrapped handler itself.** The summary
      is logged to the SAME handler that's dropping records. If the handler
      is erroring, the summary will likely error too — but ``emit_summary``
      catches that and reports it to ``fallback_logger`` instead of looping.
      This means summaries are visible exactly when the sink is healthy,
      which is the only time they're useful anyway.
    * **Only a dropped-record summary is ever synthesized.** We never
      synthesize records for successful emissions — that would double the
      traffic and isn't this library's job.
    """

    def __init__(
        self,
        wrapped: logging.Handler,
        *,
        flush_interval: float = 60.0,
        clock: Optional[Callable[[], float]] = None,
        fallback_logger_name: str = "log_record_loss_counter_utils",
    ) -> None:
        super().__init__(wrapped.level)
        self._wrapped = wrapped
        self._flush_interval = float(flush_interval)
        if self._flush_interval < 0:
            raise ValueError(
                "flush_interval must be non-negative, got {}".format(
                    self._flush_interval
                )
            )
        self._clock: Callable[[], float] = clock if clock is not None else self._default_clock
        self._dropped: int = 0
        self._last_flush: float = self._clock()
        self._fallback_logger = logging.getLogger(fallback_logger_name)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    @property
    def wrapped(self) -> logging.Handler:
        """The underlying handler we're counting losses for."""
        return self._wrapped

    @property
    def flush_interval(self) -> float:
        return self._flush_interval

    @property
    def dropped_since_last_flush(self) -> int:
        """How many records have been dropped since the most recent flush."""
        return self._dropped

    def maybe_flush(self) -> Optional[LossSummary]:
        """Emit a summary record if the interval has elapsed.

        Returns the :class:`LossSummary` that was emitted, or ``None`` if the
        interval hasn't elapsed yet. Always resets ``dropped_since_last_flush``
        to zero after a real flush, even if some of those were lost again
        during the summary emission — counting them again would risk an
        infinite feedback loop.
        """
        now = self._clock()
        if now - self._last_flush < self._flush_interval:
            return None
        return self._do_flush(now)

    def force_flush(self) -> LossSummary:
        """Flush a summary regardless of the interval. Always resets."""
        now = self._clock()
        return self._do_flush(now)

    # ------------------------------------------------------------------ #
    # logging.Handler overrides
    # ------------------------------------------------------------------ #

    def emit(self, record: logging.LogRecord) -> None:
        """Pass ``record`` to the wrapped handler; count drops on error."""
        try:
            self._wrapped.emit(record)
        except Exception:
            # Intentionally broad: any exception from the wrapped handler
            # counts as a dropped record. We deliberately do NOT chain or
            # re-raise — the whole point is to convert per-record failures
            # into an aggregate count.
            self._dropped += 1
        finally:
            self.maybe_flush()

    def flush(self) -> None:
        """Flush both the wrapped handler and a loss summary if due.

        ``logging.Handler.flush`` is not allowed to return a value in the
        stdlib's protocol, so this wraps :meth:`maybe_flush` and discards
        the summary. Call :meth:`force_flush` if you need the summary object.
        """
        self.maybe_flush()
        try:
            self._wrapped.flush()
        except Exception:
            # If flushing the underlying sink fails, we have nowhere to
            # send the summary anyway; just record the drop count.
            self._dropped += 1

    def close(self) -> None:
        # Flush whatever we have before closing so the last interval's
        # losses are visible.
        try:
            self.force_flush()
        except Exception:
            pass
        try:
            self._wrapped.close()
        except Exception:
            pass
        finally:
            super().close()

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _do_flush(self, now: float) -> LossSummary:
        dropped = self._dropped
        self._dropped = 0
        self._last_flush = now
        summary = LossSummary(dropped=dropped, flushed_at=now)
        if dropped > 0:
            self._emit_summary_record(summary)
        return summary

    def _emit_summary_record(self, summary: LossSummary) -> None:
        """Send one summary record through the wrapped handler.

        If the wrapped handler is still erroring — which is likely, given
        that we only emit summaries when records have been dropping — we
        report to the fallback logger instead of looping. The dropped count
        is NOT incremented for a summary-emission failure: doing so would
        make every subsequent flush re-emit the same losses forever.
        """
        record = logging.LogRecord(
            name="log_record_loss_counter_utils.loss_summary",
            level=logging.WARNING,
            pathname=__file__,
            lineno=0,
            msg="dropped %d log records since last flush",
            args=(summary.dropped,),
            exc_info=None,
        )
        record.created = summary.flushed_at
        record.msecs = 0.0
        try:
            self._wrapped.emit(record)
        except Exception:
            self._fallback_logger.warning(
                "log_record_loss_counter_utils: %d records dropped and "
                "summary emission also failed",
                summary.dropped,
            )

    @staticmethod
    def _default_clock() -> float:
        import time

        return time.time()
