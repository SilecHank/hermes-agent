"""Turn-local limits for the IVD answer plane."""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass


@dataclass
class IVDTurnBudget:
    mode: str
    max_searches: int
    searches: int = 0


_CURRENT_BUDGET: ContextVar[IVDTurnBudget | None] = ContextVar(
    "hermes_ivd_turn_budget",
    default=None,
)


def begin_ivd_answer_turn(*, max_searches: int, mode: str = "answer") -> Token:
    budget = IVDTurnBudget(mode=mode, max_searches=max(1, int(max_searches)))
    return _CURRENT_BUDGET.set(budget)


def end_ivd_answer_turn(token: Token) -> None:
    _CURRENT_BUDGET.reset(token)


def consume_ivd_search() -> tuple[bool, int, int]:
    budget = _CURRENT_BUDGET.get()
    if budget is None or budget.mode != "answer":
        return True, 0, 0
    budget.searches += 1
    return budget.searches <= budget.max_searches, budget.searches, budget.max_searches
