"""Strict execution state machine for backup runs."""

from __future__ import annotations

from .models import RunState


class InvalidStateTransition(ValueError):
    pass


_SUCCESS_PATH = (
    RunState.SCHEDULED,
    RunState.QUEUED,
    RunState.PRECHECK,
    RunState.PREPARING,
    RunState.BACKING_UP,
    RunState.TRANSFERRING,
    RunState.VERIFYING,
    RunState.FINALIZING,
    RunState.SUCCESS,
)

_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    state: frozenset({next_state, RunState.CLEANUP})
    for state, next_state in zip(_SUCCESS_PATH, _SUCCESS_PATH[1:])
}
_TRANSITIONS[RunState.CLEANUP] = frozenset({RunState.FAILED})
_TRANSITIONS[RunState.SUCCESS] = frozenset()
_TRANSITIONS[RunState.RECOVERING] = frozenset({
    RunState.BACKING_UP,
    RunState.CLEANUP,
})

_TRANSITIONS[RunState.FAILED] = frozenset()


def allowed_transitions(state: RunState) -> frozenset[RunState]:
    return _TRANSITIONS[state]


def validate_transition(current: RunState, target: RunState) -> None:
    if target not in allowed_transitions(current):
        raise InvalidStateTransition(f"cannot transition from {current} to {target}")
