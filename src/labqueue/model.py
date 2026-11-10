
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class QueueItem:
    student: str
    branch: str
    pr_number: int | None
    pr_url: str
    title: str
    updated_at: str
    age: str
    commits: int | None
    additions: int | None
    deletions: int | None
    review_state: str
    draft: bool
    commits_all: int | None = None
    speed: tuple[int, int, int] | None = None
    waiting_since: str = ""
    student_after: str = ""
    unresolved: int | None = None
    checks: str = "—"
    checks_url: str = ""


@dataclass
class StudentStats:
    student: str
    lab_numbers: list[int] = field(default_factory=list)
    open_prs: int = 0
    merged: int = 0
    declined: int = 0
    awaiting_commits: int = 0
    lines_add: int = 0
    lines_del: int = 0
    commits_total: int = 0
    pushes: int = 0


def format_age(dt: datetime, now: datetime) -> tuple[str, float]:
    delta = now - dt
    return (f"{delta.days}д{f'{delta.seconds // 3600}ч' if delta.seconds >= 3600 else ''}", delta.total_seconds() / 3600)
