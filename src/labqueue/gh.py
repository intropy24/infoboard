
import re
from collections.abc import Iterator
from datetime import datetime

from githubkit import GitHub

from .config import LAB_BRANCH_RE, REVIEW_MARKS


def checks_status(runs) -> str:
    latest = {}
    for r in runs:
        if r.app is None or r.app.slug != "github-actions":
            continue
        prev = latest.get(r.name)
        if prev is None or (r.id or 0) > (prev.id or 0):
            latest[r.name] = r
    live = [r for r in latest.values() if r.conclusion not in ("skipped", "neutral")]
    if not live:
        return "—"
    bad = sorted(str(r.name) for r in live if r.conclusion in ("failure", "timed_out", "cancelled", "action_required"))
    if bad:
        return "❌ " + ", ".join(bad)
    if any(r.status != "completed" for r in live):
        return "⏳ идёт"
    return "✅"


def commit_is_teacher(c, teacher: str) -> bool:
    login = c.author.login if c.author is not None else None
    email = c.commit.author.email if c.commit.author is not None else None
    return login == teacher or bool(email) and (
        re.fullmatch(rf"([0-9]+\+)?{re.escape(teacher)}@users\.noreply\.github\.com", email) is not None
    )


def iter_branches(gh: GitHub, owner: str, repo: str) -> Iterator[tuple[str, str]]:
    page = 1
    while True:
        branches = gh.rest.repos.list_branches(owner, repo, per_page=100, page=page).parsed_data
        yield from ((b.name, b.commit.sha) for b in branches)
        if len(branches) < 100:
            return
        page += 1


def iter_pulls(gh: GitHub, owner: str, repo: str) -> Iterator:
    page = 1
    while True:
        pulls = gh.rest.pulls.list(owner, repo, state="all", per_page=100, page=page).parsed_data
        yield from pulls
        if len(pulls) < 100:
            return
        page += 1


def review_summary(reviews: list, teacher: str) -> tuple[str, datetime | None, str | None, list[datetime]]:
    foreign = [
        r.submitted_at
        for r in reviews
        if (r.user is None or r.user.login != teacher) and isinstance(r.submitted_at, datetime)
    ]
    mine = [r for r in reviews if r.user is not None and r.user.login == teacher]
    if not mine:
        return "—", None, None, foreign
    last = mine[-1]
    mark = REVIEW_MARKS.get(str(last.state).upper(), str(last.state).lower())
    return (
        mark,
        last.submitted_at if isinstance(last.submitted_at, datetime) else None,
        last.commit_id if isinstance(last.commit_id, str) else None,
        foreign,
    )


def student_comment_after(
    teacher: str, since: datetime, review_times: list[datetime], issue_comments: list, review_comments: list
) -> datetime | None:
    times = [t for t in review_times if t > since]
    for batch in (issue_comments, review_comments):
        times += [
            c.created_at
            for c in batch
            if c.user is not None and c.user.login != teacher
            and isinstance(c.created_at, datetime) and c.created_at > since
        ]
    return max(times) if times else None


def pushed_at(repo_events: list[dict], head_sha: str) -> datetime | None:
    dates = [datetime.fromisoformat(e["date"]) for e in repo_events if e["head"] == head_sha]
    return max(dates) if dates else None


def commit_line_stats(gh: GitHub, owner: str, repo: str, sha: str, cache: dict, parsed=None) -> tuple[int, int]:
    key = f"{owner}/{repo}/{sha}"
    hit = cache.get(key)
    if hit is not None:
        return hit[0], hit[1]
    parsed = parsed if parsed is not None else gh.rest.repos.get_commit(owner, repo, sha).parsed_data
    st = parsed.stats
    add = getattr(st, "additions", None)
    dele = getattr(st, "deletions", None)
    add, dele = (add if isinstance(add, int) else 0, dele if isinstance(dele, int) else 0)
    cache[key] = [add, dele]
    return add, dele


def unresolved_threads(gh: GitHub, owner: str, repo: str, number: int) -> int | None:
    query = (
        "query($o:String!,$r:String!,$n:Int!){ repository(owner:$o,name:$r)"
        "{ pullRequest(number:$n){ reviewThreads(first:100){ nodes{ isResolved } } } } }"
    )
    try:
        data = gh.graphql(query, {"o": owner, "r": repo, "n": number})
        nodes = data["repository"]["pullRequest"]["reviewThreads"]["nodes"]
    except Exception:
        return None
    return sum(1 for t in nodes if not t.get("isResolved"))


def student_push_count(repo_events: list[dict], student: str, student_shas: set[str]) -> int:
    n = 0
    for e in repo_events:
        if not LAB_BRANCH_RE.match((e.get("ref") or "").removeprefix("refs/heads/")):
            continue
        if (actor := e.get("actor")) is not None:
            n += actor == student
        elif e.get("head") in student_shas:
            n += 1
    return n


def merge_events(events_cache: dict, owner: str, repo: str, pages: list[list]) -> list[dict]:
    known = events_cache.setdefault(f"{owner}/{repo}", [])
    by_id = {e["id"]: e for e in known}
    for events in pages:
        for ev in events:
            if ev.type not in ("PushEvent", "CreateEvent") or ev.created_at is None:
                continue
            ref = getattr(ev.payload, "ref", None)
            if ev.type == "CreateEvent" and getattr(ev.payload, "ref_type", None) != "branch":
                continue
            actor = ev.actor.login if ev.actor is not None else None
            if old := by_id.get(ev.id):
                if not old.get("ref") and ref:
                    old["ref"] = ref
                if not old.get("actor") and actor:
                    old["actor"] = actor
                continue
            entry = {
                "id": ev.id,
                "date": ev.created_at.isoformat(),
                "head": getattr(ev.payload, "head", None),
                "size": getattr(ev.payload, "size", None) or 1,
                "ref": ref,
                "actor": actor,
            }
            known.append(entry)
            by_id[ev.id] = entry
    return known
