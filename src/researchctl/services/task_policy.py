from __future__ import annotations

from researchctl.domain.enums import TaskState


TASK_STATE_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PLANNED: frozenset(
        {TaskState.PLANNED, TaskState.READY, TaskState.CANCELED}
    ),
    TaskState.READY: frozenset(
        {TaskState.READY, TaskState.ACTIVE, TaskState.CANCELED}
    ),
    TaskState.ACTIVE: frozenset(
        {
            TaskState.ACTIVE,
            TaskState.BLOCKED,
            TaskState.NEEDS_REVIEW,
            TaskState.CANCELED,
        }
    ),
    TaskState.BLOCKED: frozenset(
        {
            TaskState.BLOCKED,
            TaskState.ACTIVE,
            TaskState.NEEDS_REVIEW,
            TaskState.CANCELED,
        }
    ),
    TaskState.NEEDS_REVIEW: frozenset(
        {
            TaskState.NEEDS_REVIEW,
            TaskState.ACTIVE,
            TaskState.DONE,
            TaskState.CANCELED,
        }
    ),
    TaskState.DONE: frozenset({TaskState.DONE}),
    TaskState.CANCELED: frozenset({TaskState.CANCELED}),
}


def task_transition_allowed(current: TaskState, replacement: TaskState) -> bool:
    return replacement in TASK_STATE_TRANSITIONS[current]
