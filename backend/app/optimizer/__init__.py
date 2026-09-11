"""Phase 3 optimizer: a pure function that turns sessions + grid data into a charging schedule.

Public API: ``optimize(inp: OptimizerInput) -> OptimizerResult`` and the three dataclasses
``SessionInput``, ``OptimizerInput``, ``OptimizerResult``.

``optimize`` is re-exported lazily (PEP 562 module ``__getattr__``): ``app.optimizer.engine``
(and with it PuLP) is imported on first access of ``app.optimizer.optimize``, not when the
package is imported. So ``app.optimizer.types`` and ``app.optimizer.fixtures`` import on their
own, without the solver.
"""
from typing import TYPE_CHECKING, Any

from app.optimizer.types import OptimizerInput, OptimizerResult, SessionInput

if TYPE_CHECKING:  # static analysers see the eager import; at runtime it is lazy (below)
    from app.optimizer.engine import optimize

__all__ = ["optimize", "SessionInput", "OptimizerInput", "OptimizerResult"]


def __getattr__(name: str) -> Any:
    if name == "optimize":
        from app.optimizer.engine import optimize

        globals()["optimize"] = optimize  # later lookups skip this hook
        return optimize
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
