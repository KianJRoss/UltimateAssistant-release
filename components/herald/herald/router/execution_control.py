"""Per-call native process cancellation; never stored in global provider profiles."""
from contextvars import ContextVar
execution_cancel = ContextVar('herald_execution_cancel', default=None)

execution_progress = ContextVar('herald_execution_progress', default=None)
