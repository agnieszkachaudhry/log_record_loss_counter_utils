"""log_record_loss_counter_utils
============================

Wraps a logging handler and counts records dropped due to errors, flushing
the count as a periodic summary record.
"""

from .core import LossCounterHandler, LossSummary

__all__ = ["LossCounterHandler", "LossSummary"]
__version__ = "0.1.0"
