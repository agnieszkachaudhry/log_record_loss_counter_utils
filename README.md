# log_record_loss_counter_utils

Wraps a `logging.Handler` and counts records dropped due to errors, flushing
the count as a periodic summary record.

```python
import logging
from log_record_loss_counter_utils import LossCounterHandler

sink = logging.FileHandler("app.log")
counter = LossCounterHandler(sink, flush_interval=60.0)
logger = logging.getLogger("app")
logger.addHandler(counter)
logger.setLevel(logging.INFO)

logger.info("hello")          # passes through to app.log
# if app.log is unwritable, the failed emit is counted silently;
# after 60 seconds a single WARNING summary record is emitted to app.log
# describing how many records were lost.
```

## Why this exists

When a downstream log sink (file, socket, HTTP endpoint) fails, the stdlib's
default `Handler.emit` swallows the exception *and* prints a traceback to
stderr for every single record. Under sustained failure that's both noisy and
useless — the operator wants an aggregate ("you lost 4,213 records in the
last minute"), not 4,213 identical stack traces.

This handler converts per-record failures into a counter and periodically
flushes one summary record. The trade-off is explicit: you lose per-record
error detail in exchange for not being spammed. The summaries are emitted to
the *same* wrapped handler, so they're visible exactly when the sink is
healthy — which is the only time they're useful.

## Edge you'll hit

If the wrapped handler is still erroring at flush time, the summary record
can't be delivered either. In that case `LossCounterHandler` falls back to a
stdlib `logging.getLogger("log_record_loss_counter_utils")` warning and
*resets the counter anyway*. This is deliberate: re-counting dropped
summaries would create an unbounded feedback loop where every flush re-emits
all prior losses. The cost is that a permanently-broken sink will lose its
summary too — the fallback logger is your only signal in that case.

The flush cadence is driven by a `clock` callable (default `time.time`) so
it's deterministic in tests; pass a fake clock to control when summaries
fire.

## Exported names

- `LossCounterHandler(wrapped, *, flush_interval=60.0, clock=None, fallback_logger_name="log_record_loss_counter_utils")`
  — the wrapper handler. Inherits `logging.Handler`.
  - `.wrapped` — the underlying handler.
  - `.flush_interval` — seconds between automatic summary flushes.
  - `.dropped_since_last_flush` — int, losses since the last summary.
  - `.maybe_flush()` — returns `LossSummary | None`; emits a summary if the
    interval has elapsed.
  - `.force_flush()` — returns `LossSummary`; always flushes.
- `LossSummary` — value object with `.dropped: int` and `.flushed_at: float`.

Negative `flush_interval` raises `ValueError`. `flush_interval=0.0` flushes on
every emit.
