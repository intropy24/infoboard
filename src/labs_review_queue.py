
import argparse
import json
import re
import sys
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from itertools import pairwise
from pathlib import Path

from githubkit import GitHub
from githubkit.exception import RateLimitExceeded, RequestFailed, RequestTimeout
from pydantic import Field

from pydantic_settings import BaseSettings, SettingsConfigDict
class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    gh_pat: str = Field(..., alias="GH_PAT")

LAB_BRANCH_RE = re.compile(r"^lab-[0-9]{2}([-.].*)?$")

EVENTS_CACHE_PATH = Path(__file__).resolve().parent / "events-cache.json"
STATS_CACHE_PATH = Path(__file__).resolve().parent / "commit-stats-cache.json"

REVIEW_MARKS = {
    "APPROVED": "✅ approved",
    "CHANGES_REQUESTED": "🔄 changes",
    "COMMENTED": "💬 commented",
    "DISMISSED": "🚫 dismissed",
}


class QueueSettings(Settings):
    gh_org: str = Field("intropy24", alias="GH_ORG")


def iter_org_repos(gh: GitHub, org: str):
    page = 1
    while True:
        repos = gh.rest.repos.list_for_org(org, per_page=100, page=page).parsed_data
        yield from repos
        if len(repos) < 100:
            break
        page += 1
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


def checks_rank(item: QueueItem) -> int:
    c = item.checks
    if c.startswith("✅"):
        return 0
    if c.startswith("⏳") or c == "—":
        return 1
    return 2


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
    except Exception:  # noqa: BLE001 — второстепенная метрика: любая ошибка GraphQL даёт «—», отчёт не роняем
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


MSK = timezone(timedelta(hours=3))


def fmt_dt(iso: str) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def repo_url(org: str, student: str) -> str:
    return f"https://github.com/{org}/{org}-{student}"


def lines_per_hour(points: list[tuple[datetime, int]]) -> tuple[int, int, int] | None:
    pts = sorted(points)
    rates = []
    for (t_prev, _), (t, a) in pairwise(pts):
        if (hours := (t - t_prev).total_seconds() / 3600) > 0:
            rates.append((a, hours))
    if not rates:
        return None
    total_hours = sum(h for _, h in rates)
    avg = round(sum(a for a, _ in rates) / total_hours) if total_hours else 0
    return avg, round(max(a / h for a, h in rates)), len(rates)


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


def collect_student(
    gh: GitHub, owner: str, repo: str, prefix: str, now: datetime, events_cache: dict,
    teacher: str, stat_cache: dict, pool: ThreadPoolExecutor,
) -> tuple[list[QueueItem], list[QueueItem], list[QueueItem], StudentStats, list[dict]]:
    student = repo.removeprefix(f"{prefix}-")
    stats = StudentStats(student=student)
    student_shas: set[str] = set()
    stat_shas: set[str] = set()
    lab_chart: dict[str, dict[str, tuple[datetime, int, str]]] = {}

    def add_points(branch: str, entries: list[tuple[object, str, int, str]]) -> None:
        pts = lab_chart.setdefault(branch, {})
        for d, c_sha, a_, msg in entries:
            if not isinstance(d, datetime) or a_ <= 0 or c_sha in pts:
                continue
            pts[c_sha] = (d, a_, msg)

    def msg_line(msg: str | None) -> str:
        return ((msg or "").splitlines() or [""])[0].strip()[:80]

    def get_branches() -> list[tuple[str, str]]:
        return list(iter_branches(gh, owner, repo))

    def pr_commits(pn: int) -> list:
        out: list = []
        for page in range(1, 100):
            batch = gh.rest.pulls.list_commits(owner, repo, pn, per_page=100, page=page).parsed_data or []
            out += batch
            if len(batch) < 100:
                break
        return out

    def pr_comments(fetch, pn: int) -> list:
        out: list = []
        for page in (1, 2, 3):
            batch = fetch(owner, repo, pn, per_page=100, page=page).parsed_data
            out += batch
            if len(batch) < 100:
                break
        return out

    def get_commit_parsed(sha: str):
        return gh.rest.repos.get_commit(owner, repo, sha).parsed_data

    f_events = [pool.submit(gh.rest.activity.list_repo_events, owner, repo, per_page=100, page=p) for p in (1, 2, 3)]
    f_branches = pool.submit(get_branches)
    f_pulls = pool.submit(list, iter_pulls(gh, owner, repo))
    branches = f_branches.result()
    pr_by_branch: dict[str, list] = {}
    for pr in f_pulls.result():
        pr_by_branch.setdefault(pr.head.ref, []).append(pr)

    f_commits: dict[int, Future] = {}
    f_reviews: dict[int, Future] = {}
    f_iss: dict[int, Future] = {}
    f_rc: dict[int, Future] = {}
    f_cmp: dict[str, Future] = {}
    f_head: dict[str, Future] = {}
    f_checks: dict[str, Future] = {}
    for name, sha in branches:
        if name in ("main", "master") or not LAB_BRANCH_RE.match(name):
            continue
        f_checks[name] = pool.submit(gh.rest.checks.list_for_ref, owner, repo, sha, per_page=100)
        candidates = pr_by_branch.get(name, [])
        if not candidates:
            f_cmp[name] = pool.submit(gh.rest.repos.compare_commits, owner, repo, f"main...{sha}")
            f_head[name] = pool.submit(get_commit_parsed, sha)
            continue
        for p in candidates:
            if p.state == "open":
                f_reviews[p.number] = pool.submit(gh.rest.pulls.list_reviews, owner, repo, p.number, per_page=100)
                f_iss[p.number] = pool.submit(pr_comments, gh.rest.issues.list_comments, p.number)
                f_rc[p.number] = pool.submit(pr_comments, gh.rest.pulls.list_review_comments, p.number)
            elif p.merged_at is None:
                continue
            f_commits[p.number] = pool.submit(pr_commits, p.number)

    repo_events = merge_events(events_cache, owner, repo, [f.result().parsed_data for f in f_events])

    reviews_by_pr = {n: f.result().parsed_data for n, f in f_reviews.items()}
    iss_by_pr = {n: f.result() for n, f in f_iss.items()}
    rc_by_pr = {n: f.result() for n, f in f_rc.items()}
    commits_by_pr = {n: f.result() for n, f in f_commits.items()}
    ahead_by_branch = {n: f.result().parsed_data.commits or [] for n, f in f_cmp.items()}
    checks_by_branch = {n: checks_status(f.result().parsed_data.check_runs or []) for n, f in f_checks.items()}

    f_stats = [
        pool.submit(commit_line_stats, gh, owner, repo, c.sha, stat_cache)
        for commits in commits_by_pr.values()
        for c in commits
        if not commit_is_teacher(c, teacher) and f"{owner}/{repo}/{c.sha}" not in stat_cache
    ]
    f_getc = {
        c.sha: pool.submit(get_commit_parsed, c.sha)
        for ahead in ahead_by_branch.values()
        for c in ahead
    }
    for f in f_stats:
        f.result()
    getc = {sha: f.result() for sha, f in f_getc.items()}
    f_threads = {
        n: pool.submit(unresolved_threads, gh, owner, repo, n)
        for n, revs in reviews_by_pr.items()
        if any(r.user is not None and r.user.login == teacher for r in revs)
    }
    threads_by_pr = {n: f.result() for n, f in f_threads.items()}

    queue: list[QueueItem] = []
    reviewed: list[QueueItem] = []
    no_pr: list[QueueItem] = []
    for name, sha in branches:
        if name in ("main", "master"):
            continue
        if not LAB_BRANCH_RE.match(name):
            continue
        lab_number = int(name[4:6])
        candidates = sorted(pr_by_branch.get(name, []), key=lambda p: p.updated_at, reverse=True)
        if not candidates:
            if lab_number not in stats.lab_numbers:
                stats.lab_numbers.append(lab_number)
            own: tuple[str, datetime | None] | None = None
            for c in reversed(ahead_by_branch[name]):
                fullc = getc[c.sha]
                if commit_is_teacher(fullc, teacher):
                    continue
                student_shas.add(c.sha)
                if c.sha in stat_shas:
                    continue
                stat_shas.add(c.sha)
                stats.commits_total += 1
                a_, d_ = commit_line_stats(gh, owner, repo, c.sha, stat_cache, fullc)
                stats.lines_add += a_
                stats.lines_del += d_
                if own is None:
                    comm = fullc.commit.committer or fullc.commit.author
                    own = (c.sha, comm.date if comm is not None and isinstance(comm.date, datetime) else None)
            if own is not None:
                date = pushed_at(repo_events, own[0]) or own[1] or now
            else:
                commit = (getc[sha] if sha in getc else f_head[name].result()).commit
                committer = commit.committer or commit.author
                raw_date = committer.date if committer is not None else None
                date = pushed_at(repo_events, sha) or (raw_date if isinstance(raw_date, datetime) else now)
            age, _ = format_age(date, now)
            no_pr.append(QueueItem(
                student, name, None, "", "", date.isoformat(), age, None, None, None, "нет PR", False,
                waiting_since=date.isoformat(), checks=checks_by_branch.get(name, "—"),
                checks_url=f"https://github.com/{owner}/{repo}/commit/{sha}/checks",
            ))
            continue
        if lab_number not in stats.lab_numbers and any(
            p.state == "open" or p.merged_at is not None for p in candidates
        ):
            stats.lab_numbers.append(lab_number)
        for p in candidates:
            if p.state != "open":
                if p.merged_at is not None:
                    stats.merged += 1
                    for c in commits_by_pr[p.number]:
                        if commit_is_teacher(c, teacher):
                            continue
                        student_shas.add(c.sha)
                        if c.sha in stat_shas:
                            continue
                        stat_shas.add(c.sha)
                        stats.commits_total += 1
                        a_, d_ = commit_line_stats(gh, owner, repo, c.sha, stat_cache)
                        stats.lines_add += a_
                        stats.lines_del += d_
                    add_points(name, [
                        (c.commit.author.date if c.commit.author is not None else None, c.sha,
                         commit_line_stats(gh, owner, repo, c.sha, stat_cache)[0], msg_line(c.commit.message))
                        for c in commits_by_pr[p.number]
                        if not commit_is_teacher(c, teacher)
                    ])
                else:
                    stats.declined += 1
                continue
            commits: list[tuple[datetime, datetime, str, bool]] = []
            for c in commits_by_pr[p.number]:
                raw_a = c.commit.author.date if c.commit.author is not None else None
                raw_c = c.commit.committer.date if c.commit.committer is not None else None
                a = raw_a if isinstance(raw_a, datetime) else None
                if a is None:
                    continue
                commits.append((a, raw_c if isinstance(raw_c, datetime) else a, c.sha, commit_is_teacher(c, teacher)))
            shas = {sha for _, _, sha, _ in commits}
            ours_shas = {sha for _, _, sha, ours in commits if ours}
            branch_pushes = [
                (datetime.fromisoformat(e["date"]), e["size"])
                for e in repo_events
                if e["head"] in shas and e["head"] not in ours_shas
            ]
            student_commits = [t for t in commits if not t[3]]
            student_shas.update(sha for _, _, sha, _ in student_commits)
            line_by_sha: dict[str, tuple[int, int]] = {}
            for _, _, c_sha, _ in student_commits:
                a_, d_ = commit_line_stats(gh, owner, repo, c_sha, stat_cache)
                line_by_sha[c_sha] = (a_, d_)
                if c_sha in stat_shas:
                    continue
                stat_shas.add(c_sha)
                stats.commits_total += 1
                stats.lines_add += a_
                stats.lines_del += d_
            pr_add = sum(a for a, _ in line_by_sha.values())
            pr_del = sum(d for _, d in line_by_sha.values())
            speed = lines_per_hour([(a, line_by_sha[sha][0]) for a, _, sha, _ in student_commits])
            msgs = {c.sha: msg_line(c.commit.message) for c in commits_by_pr[p.number]}
            add_points(name, [(a, c_sha, line_by_sha[c_sha][0], msgs.get(c_sha, "")) for a, _, c_sha, _ in student_commits])
            review_mark, teacher_seen, teacher_commit, foreign_reviews = review_summary(reviews_by_pr[p.number], teacher)
            push_dates = sorted(d for d, _ in branch_pushes)
            if isinstance(teacher_seen, datetime):
                push_after: datetime | None = None
                later = [d for d in push_dates if d > teacher_seen]
                push_after = later[-1] if later else None
                arrived_after = push_after is not None or (
                    bool(student_commits) and student_commits[-1][1] > teacher_seen
                )
                if teacher_commit is not None:
                    changed = teacher_commit != p.head.sha and arrived_after
                else:
                    changed = arrived_after
                if not changed:
                    comment = student_comment_after(
                        teacher, teacher_seen, foreign_reviews, iss_by_pr[p.number], rc_by_pr[p.number]
                    )
                    age, _ = format_age(teacher_seen, now)
                    reviewed.append(QueueItem(
                        student=student,
                        branch=name,
                        pr_number=p.number,
                        pr_url=p.html_url,
                        title=p.title,
                        updated_at=p.updated_at.isoformat(),
                        age=age,
                        commits=len(student_commits),
                        commits_all=len(student_commits),
                        additions=pr_add if student_commits else None,
                        deletions=pr_del if student_commits else None,
                        review_state=review_mark,
                        draft=bool(p.draft),
                        speed=speed,
                        waiting_since=teacher_seen.isoformat(),
                        student_after=comment.isoformat() if comment else "",
                        unresolved=threads_by_pr.get(p.number),
                        checks=checks_by_branch.get(name, "—"),
                        checks_url=f"https://github.com/{owner}/{repo}/commit/{sha}/checks",
                    ))
                    continue
                if push_after is None and student_commits and student_commits[-1][1] > teacher_seen:
                    push_after = student_commits[-1][1]
                waiting_start = push_after if push_after is not None else teacher_seen
                order = [sha for _, _, sha, _ in commits]
                if teacher_commit is not None and teacher_commit in order:
                    pend = [t for t in commits[order.index(teacher_commit) + 1 :] if not t[3]]
                else:
                    pend = [t for t in commits if not t[3] and t[1] > teacher_seen]
                pend = pend or student_commits
            else:
                starts = [d for d in (*push_dates, p.created_at) if isinstance(d, datetime)]
                waiting_start = max(starts) if starts else p.updated_at
                pend = student_commits
            pending = len(pend)
            pend_add = sum(line_by_sha[sha][0] for _, _, sha, _ in pend)
            pend_del = sum(line_by_sha[sha][1] for _, _, sha, _ in pend)
            age, _ = format_age(waiting_start, now)
            stats.open_prs += 1
            stats.awaiting_commits += pending
            queue.append(QueueItem(
                student=student,
                branch=name,
                pr_number=p.number,
                pr_url=p.html_url,
                title=p.title,
                updated_at=p.updated_at.isoformat(),
                age=age,
                commits=len(pend),
                commits_all=len(student_commits),
                additions=pend_add if pend else None,
                deletions=pend_del if pend else None,
                review_state=review_mark,
                draft=bool(p.draft),
                speed=speed,
                waiting_since=waiting_start.isoformat(),
                checks=checks_by_branch.get(name, "—"),
                checks_url=f"https://github.com/{owner}/{repo}/commit/{sha}/checks",
            ))
    charts: list[dict] = []
    for name, pts in lab_chart.items():
        points: list[list] = []
        prev: datetime | None = None
        for c_sha, (d, a_, msg) in sorted(pts.items(), key=lambda e: e[1][0]):
            if prev is not None and (h := (d - prev).total_seconds() / 3600) > 0:
                points.append([int(d.timestamp() * 1000), max(1, round(a_ / h)), f"https://github.com/{owner}/{repo}/commit/{c_sha}", c_sha[:7], msg, a_])
            prev = d
        if points:
            charts.append({"s": student, "b": name, "p": points})
    stats.pushes = student_push_count(repo_events, student, student_shas)
    return queue, reviewed, no_pr, stats, charts


def _pad(s: str, width: int) -> str:
    w = sum(
        2 if 0x2300 <= ord(c) <= 0x27BF or 0x2B00 <= ord(c) <= 0x2BFF or 0x1F000 <= ord(c) <= 0x1FAFF else 1
        for c in s
    )
    return s + " " * max(0, width - w)


def report(
    queue: list[QueueItem], reviewed: list[QueueItem], no_pr: list[QueueItem], stats: list[StudentStats], org: str, failed: int
) -> None:
    queue.sort(key=lambda item: (checks_rank(item), item.waiting_since or item.updated_at))
    print("=== Очередь на проверку ===")
    if not queue:
        print("  пусто — все(PR'ы) проверены")
    for i, item in enumerate(queue, start=1):
        draft = " 🚧draft" if item.draft else ""
        plus_minus = f"+{item.additions}/-{item.deletions}" if item.additions is not None else "?"
        speed = f"  ⚡{item.speed[0]}/{item.speed[1]} ({item.speed[2]} инт.)" if item.speed else ""
        since = f" с {fmt_dt(item.waiting_since)}" if item.waiting_since else ""
        comm = item.commits if item.commits is not None else "?"
        if item.commits_all is not None:
            comm = f"{comm}/{item.commits_all}"
        print(
            f"{i:>3}. ⏳ {item.age:<8} {item.student:<18} {item.branch:<26} "
            f"{_pad(f'⬇{comm}', 8)} {_pad(plus_minus, 12)} "
            f"{_pad(f'{item.review_state}{draft}', 16)} {_pad(item.checks, 26)} {since}{speed}"
        )

    if reviewed:
        silent = sorted((it for it in reviewed if not it.student_after), key=lambda it: it.waiting_since)
        answered = sorted((it for it in reviewed if it.student_after), key=lambda it: it.waiting_since)
        if silent:
            print("\n=== Ожидаются исправления ===")
            for i, item in enumerate(silent, start=1):
                threads = "—" if item.unresolved is None else f"{item.unresolved}"
                print(
                    f"{i:>3}. 🔕 {item.age:<8} {item.student:<18} {item.branch:<26} "
                    f"{_pad(item.review_state, 16)}— ревью с {fmt_dt(item.waiting_since)}, 🧵 convs.: {threads:>2}"
                )
        if answered:
            print("\n=== Прокомментировано студентом ===")
            for i, item in enumerate(answered, start=1):
                print(
                    f"{i:>3}. 💬 {item.age:<8} {item.student:<18} {item.branch:<26} "
                    f"{_pad(item.review_state, 16)}— ревью с {fmt_dt(item.waiting_since)}, ответ студента с {fmt_dt(item.student_after)}"
                )

    if no_pr:
        print("\n=== Lab-ветки без PR ===")
        for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at):
            print(f"      ⏳ {item.age:<8} {item.student:<18} {item.branch} (последний коммит {fmt_dt(item.updated_at)}, CI: {item.checks})")

    print(f"\n=== Статистика по студентам ({org}) ===")
    header = (
        f"{'№':>2} {'студент':<18} {'лабы':<9} {'ждут':>4} {'слито':>6} {'откл':>5} "
        f"{'комм. на проверку':>17} {'строк +/-':>12} {'коммитов':>9} {'пушей':>6}"
    )
    print(header)
    for i, st in enumerate(sorted(stats, key=_stat_order), start=1):
        labs = ",".join(str(n) for n in sorted(st.lab_numbers)) or "—"
        lines = f"+{st.lines_add}/-{st.lines_del}" if (st.lines_add or st.lines_del) else "—"
        print(
            f"{i:>2} {st.student:<18} {labs:<9} {(st.open_prs or '—'):>4} {(st.merged or '—'):>6} "
            f"{(st.declined or '—'):>5} {(st.awaiting_commits or '—'):>17} {lines:>12} "
            f"{(st.commits_total or '—'):>9} {(st.pushes or '—'):>6}"
        )

    in_queue = len(queue)
    print(
        f"\nИтого: студентов {len(stats)}, PR'ов на проверке {in_queue}, "
        f"веток без PR {len(no_pr)}, ждёт проверки коммитов {sum(s.awaiting_commits for s in stats)}"
        + (f", репо с ошибками {failed}" if failed else "")
    )


def _stat_order(s: StudentStats) -> tuple:
    return (-(s.lines_add + s.lines_del), -s.commits_total, s.student)


def _md_checks(item: QueueItem) -> str:
    s = item.checks
    if s.startswith("❌ "):
        s = f"❌ {len(s[2:].split(', '))}"
    elif s == "⏳ идёт":
        s = "⏳"
    return f'<a class="ci" href="{item.checks_url}">{s}</a>' if item.checks_url and s != "—" else s


SPEED_JS = """
(function () {
  var el = document.getElementById('speed-data');
  if (!el || !window.Chart) return;
  var DATA = JSON.parse(el.textContent);
  function hueOf(name) { // стабильный цвет студента: хэш ника в тон палитры
    var h = 0;
    for (var i = 0; i < name.length; i++) h = (h * 31 + name.charCodeAt(i)) % 360;
    return h;
  }
  var fmtTick = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });
  var fmtTip = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  var state = { focus: null }; // студент, на чьём имени сейчас курсор списка
  var mode = 'rate'; // 'rate' — стр/ч, 'lines' — строк в коммите
  function alpha(d) { // при наведении: кривые студента почти сплошные, остальные едва видны
    if (state.focus) return d.student === state.focus ? 0.95 : 0.04;
    return 0.85;
  }
  function col(ctx) { // scriptable-цвет: пересчитывается при каждой отрисовке, поэтому
    var d = ctx.dataset; // и линии, и точки всегда ровно в цвете студента (в тёмной теме тоже)
    var l = state.focus && d.student === state.focus ? 62 : 55;
    return 'hsla(' + d.hue + ',72%,' + l + '%,' + alpha(d) + ')';
  }
  var ds = DATA.map(function (d) {
    return {
      label: d.s + ' · ' + d.b, student: d.s, branch: d.b, hue: hueOf(d.s),
      data: d.p.map(function (pt) { return { x: pt[0], y: pt[1], r: pt[1], l: pt[5], u: pt[2], h: pt[3], m: pt[4] }; }),
      borderColor: col, backgroundColor: col, pointBackgroundColor: col,
      pointBorderColor: 'transparent', pointBorderWidth: 0,
      pointRadius: 2.5, pointHoverRadius: 4.5, borderWidth: 1.4, tension: 0
    };
  });
  var drag = null; // идёт выделение рамкой: {x0, x1} в пикселях канваса
  var draggedOnce = false; // после перетаскивания подавляем клик, иначе открылся бы коммит
  var cursor = null; // курсор над полем графика: {x, y} — для перекрестия
  var crosshair = { // две полупрозрачные пунктирные линии по курсору, как в графане
    id: 'sp-crosshair',
    afterDraw: function (ch) {
      if (!cursor) return;
      var c2 = ch.ctx, ar = ch.chartArea;
      c2.save();
      c2.strokeStyle = 'rgba(139,148,158,.55)';
      c2.setLineDash([4, 4]);
      c2.beginPath(); c2.moveTo(cursor.x + .5, ar.top); c2.lineTo(cursor.x + .5, ar.bottom); c2.stroke();
      c2.beginPath(); c2.moveTo(ar.left, cursor.y + .5); c2.lineTo(ar.right, cursor.y + .5); c2.stroke();
      c2.restore();
    }
  };
  var dragRect = { // полупрозрачная рамка выделения прямо на канвасе, по-графановски
    id: 'sp-drag-rect',
    afterDraw: function (ch) {
      if (!drag) return;
      var c2 = ch.ctx, ar = ch.chartArea, a = Math.min(drag.x0, drag.x1), w = Math.abs(drag.x1 - drag.x0);
      c2.save();
      c2.fillStyle = 'rgba(56,139,253,.15)';
      c2.fillRect(a, ar.top, w, ar.bottom - ar.top);
      c2.strokeStyle = 'rgba(56,139,253,.7)';
      c2.strokeRect(a + .5, ar.top + .5, w, ar.bottom - ar.top - 1);
      c2.restore();
    }
  };
  var chart = new Chart(document.getElementById('speed-canvas'), {
    type: 'line',
    data: { datasets: ds },
    plugins: [dragRect, crosshair],
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      // режим 'x': подсветка коммита — только когда вертикаль пересекла его (как перекрестие)
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      // точка = коммит: клик открывает страницу коммита
      onClick: function (e, els, ch) {
        if (draggedOnce) { draggedOnce = false; return; }
        if (!els.length) return;
        var raw = ch.data.datasets[els[0].datasetIndex].data[els[0].index];
        if (raw && raw.u) window.open(raw.u, '_blank');
      },
      onHover: function (e, els) { if (e.native && e.native.target && !drag) e.native.target.style.cursor = els.length ? 'pointer' : 'default'; },
      scales: {
        x: { type: 'linear',
             ticks: { maxRotation: 0, autoSkip: true, callback: function (v) { return fmtTick.format(new Date(v)); } },
             grid: { color: 'rgba(128,128,128,.15)' } },
        y: { type: 'logarithmic', title: { display: true, text: 'скорость коммита, стр/ч (лог)' },
             ticks: { callback: function (v) { // подписываем только 1,2,5 · 10^n, остальные деления прячем
                 var r = Math.round(Math.log10(v)), q = v / Math.pow(10, r);
                 if (Math.abs(q - 1) < 1e-9 || Math.abs(q - 2) < 1e-9 || Math.abs(q - 5) < 1e-9) return String(Math.round(v));
                 return null; } },
             grid: { color: 'rgba(128,128,128,.15)' } }
      },
      plugins: {
        legend: { display: false },
        tooltip: { callbacks: {
          title: function (items) { return items.length ? fmtTip.format(new Date(items[0].parsed.x)) : ''; },
          label: function (ctx) { var r = ctx.raw; return mode === 'rate' ? ctx.dataset.label + ': +' + r.l + ' строк → ' + ctx.parsed.y + ' стр/ч' : ctx.dataset.label + ': +' + r.l + ' строк'; },
          afterLabel: function (ctx) { return ctx.raw.m ? '«' + ctx.raw.m + '»' : ''; } } }
      }
    }
  });
  var selL = document.getElementById('sp-lab');
  var picked = {}; // клики по списку студентов работают как чекбоксы: пусто = показываем всех
  var labs = [];
  DATA.forEach(function (d) { if (labs.indexOf(d.b) < 0) labs.push(d.b); });
  labs.sort();
  function apply() {
    var any = Object.keys(picked).length > 0;
    ds.forEach(function (d) {
      d.hidden = !((!any || picked[d.student]) && (!selL.value || d.branch === selL.value));
    });
    chart.update('none');
  }
  (function () {
    var o = document.createElement('option'); o.value = ''; o.textContent = 'все лабы'; selL.appendChild(o);
    labs.forEach(function (v) { var e = document.createElement('option'); e.value = v; e.textContent = v; selL.appendChild(e); });
    selL.onchange = apply;
  })();
  var btns = document.querySelectorAll('#sp-mode button');
  function setMode(m) { // метрика Y переключается на лету: обе величины лежат в каждой точке
    mode = m;
    ds.forEach(function (d) { d.data.forEach(function (p) { p.y = m === 'rate' ? p.r : p.l; }); });
    chart.options.scales.y.title.text = m === 'rate' ? 'скорость коммита, стр/ч (лог)' : 'строк в коммите (лог)';
    Array.prototype.forEach.call(btns, function (b) { b.classList.toggle('on', b.getAttribute('data-mode') === m); });
    chart.update('none');
  }
  Array.prototype.forEach.call(btns, function (b) { b.onclick = function () { setMode(b.getAttribute('data-mode')); }; });
  // выделение отрезка времени: тянем рамку по графику -> зум по X и заполнение «с/по»;
  // диапазон можно ввести и руками, сброс — кнопкой ×
  var inpF = document.getElementById('sp-from'), inpT = document.getElementById('sp-to');
  function pad2(n) { return (n < 10 ? '0' : '') + n; }
  function toInput(ms) { // dd.mm.yyyy hh:mm — привычный формат; пусто = без границы
    if (ms == null) return '';
    var d = new Date(ms);
    return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  function parseInput(v) { // тот же формат руками: null — пусто, NaN — мусор (подсветим красным)
    v = (v || '').trim();
    if (!v) return null;
    var m = v.match(/^(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{4})(?:[ ,]+(\d{1,2})(?::(\d{1,2}))?)?$/);
    if (!m) return NaN;
    var d = new Date(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0));
    // 31.06. -> Date молча перекатывает на июль — считаем это мусором
    if (d.getFullYear() !== +m[3] || d.getMonth() !== +m[2] - 1 || d.getDate() !== +m[1]) return NaN;
    return d.getTime();
  }
  function applyTime(from, to) { // мс или null — границы окна по X
    chart.options.scales.x.min = from == null ? undefined : from;
    chart.options.scales.x.max = to == null ? undefined : to;
    inpF.value = toInput(from); inpT.value = toInput(to);
    chart.update('none');
  }
  function onInputTime() {
    var f = parseInput(inpF.value), t = parseInput(inpT.value);
    inpF.classList.toggle('bad', isNaN(f));
    inpT.classList.toggle('bad', isNaN(t));
    if (isNaN(f) || isNaN(t)) return;
    if (f != null && t != null && f >= t) return; // перевёрнутый диапазон — ждём доводки
    applyTime(f, t);
  }
  inpF.onchange = onInputTime; inpT.onchange = onInputTime;
  inpF.onkeydown = inpT.onkeydown = function (e) { if (e.key === 'Enter') onInputTime(); };
  document.getElementById('sp-reset').onclick = function () { applyTime(null, null); };
  var canvas = chart.canvas;
  canvas.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    var r = canvas.getBoundingClientRect();
    drag = { x0: e.clientX - r.left, x1: e.clientX - r.left };
    canvas.style.cursor = 'crosshair';
  });
  canvas.addEventListener('mousemove', function (e) {
    var r = canvas.getBoundingClientRect(), ar = chart.chartArea;
    var x = e.clientX - r.left, y = e.clientY - r.top;
    cursor = x >= ar.left && x <= ar.right && y >= ar.top && y <= ar.bottom ? { x: x, y: y } : null;
    if (drag) drag.x1 = Math.max(ar.left, Math.min(ar.right, x));
    chart.render();
  });
  canvas.addEventListener('mouseleave', function () { if (cursor) { cursor = null; chart.render(); } });
  window.addEventListener('mouseup', function () {
    if (!drag) return;
    var a = Math.min(drag.x0, drag.x1), b = Math.max(drag.x0, drag.x1);
    drag = null;
    canvas.style.cursor = 'default';
    if (b - a < 5) { chart.render(); return; } // это был обычный клик, не выделение
    draggedOnce = true;
    applyTime(chart.scales.x.getValueForPixel(a), chart.scales.x.getValueForPixel(b));
  });
  Array.prototype.forEach.call(document.getElementById('sp-list').children, function (li) {
    var s = li.getAttribute('data-student');
    li.querySelector('i').style.background = 'hsl(' + hueOf(s) + ',72%,55%)';
    li.onmouseenter = function () { state.focus = s; chart.update('none'); };
    li.onmouseleave = function () { state.focus = null; chart.update('none'); };
    li.onclick = function () { // как чекбокс: один клик — видеть только его, ещё клик — добавить ещё одного
      if (picked[s]) { delete picked[s]; li.classList.remove('on'); }
      else { picked[s] = true; li.classList.add('on'); }
      apply();
    };
  });
})();
"""


def _speed_chart(charts: list[dict]) -> None:
    data = [c for c in charts if c["p"]]
    if not data:
        return
    print("## ⚡ Коммиты — все живые PR'ы (стр/ч ↔ строк в коммите)")
    print()
    print('<div id="speed-wrap">')
    print('<div id="speed-side">')
    print('<div id="speed-controls"><select id="sp-lab"></select>')
    print('<div id="sp-mode"><button data-mode="rate" class="on">стр/ч</button><button data-mode="lines">строк в коммите</button></div>')
    print('<div id="sp-times"><label>с <input id="sp-from" type="text" placeholder="дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"></label><label>по <input id="sp-to" type="text" placeholder="дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"></label><button id="sp-reset" title="показать всё время">×</button></div>')
    print('</div>')
    print('<ul id="sp-list">')
    for s in sorted({d["s"] for d in data}):
        print(f'<li data-student="{s}"><i></i>{s}</li>')
    print('</ul>')
    print('</div>')
    print('<div id="speed-box"><canvas id="speed-canvas"></canvas></div>')
    print('</div>')
    print()
    print('<script type="application/json" id="speed-data">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + '</script>')
    print()
    print('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')
    print()
    print('<script>' + SPEED_JS + '</script>')
    print()


def report_md(
    queue: list[QueueItem], reviewed: list[QueueItem], no_pr: list[QueueItem], stats: list[StudentStats], org: str, failed: int,
    charts: list[dict],
) -> None:
    queue.sort(key=lambda item: (checks_rank(item), item.waiting_since or item.updated_at))
    print(f"# {org}")
    print()
    print(f"_Обновлено: {datetime.now(MSK):%d.%m.%Y %H:%M} МСК_")
    print()
    print("## Очередь на проверку")
    print()
    if not queue:
        print("Пусто — все PR'ы проверены ✅")
    else:
        print("| № | Студент | PR | Коммитов (ждут/всего) | +/− | Ревью | CI | Ждёт | С (МСК) | ⚡ стр/ч (ср/макс·инт) |")
        print("|---:|:---|:---|---:|---:|:---|:---|---:|:---:|:---:|")
        for i, item in enumerate(queue, start=1):
            draft = " 🚧" if item.draft else ""
            plus_minus = f"+{item.additions}/-{item.deletions}" if item.additions is not None else "—"
            speed = f"{item.speed[0]}/{item.speed[1]} ({item.speed[2]})" if item.speed else "—"
            comm = f"{item.commits}/{item.commits_all}" if item.commits_all is not None else (item.commits if item.commits is not None else "—")
            print(
                f"| {i} | <a class=\"student\" href=\"{repo_url(org, item.student)}\">{item.student}</a> | [`{item.branch}`]({item.pr_url}) | "
                f"{comm} | {plus_minus} | {item.review_state}{draft} | {_md_checks(item)} | "
                f"{item.age} | {fmt_dt(item.waiting_since) or '—'} | {speed} |"
            )
    print()
    _speed_chart(charts)

    if reviewed:
        silent = sorted((it for it in reviewed if not it.student_after), key=lambda it: it.waiting_since)
        answered = sorted((it for it in reviewed if it.student_after), key=lambda it: it.waiting_since)
        if silent:
            print("## Ожидаются исправления")
            print()
            print("| № | Студент | PR | Ревью | convs | Давность | С (МСК) |")
            print("|---:|:---|:---|:---|---:|---:|:---:|")
            for i, item in enumerate(silent, start=1):
                print(
                    f"| {i} | <a class=\"student\" href=\"{repo_url(org, item.student)}\">{item.student}</a> | [`{item.branch}`]({item.pr_url}) | "
                    f"{item.review_state} | {item.unresolved if item.unresolved is not None else '—'} | {item.age} | {fmt_dt(item.waiting_since) or '—'} |"
                )
            print()
        if answered:
            print("## Прокомментировано студентом")
            print()
            print("| № | Студент | PR | Ревью | Давность | Ревью с | Ответ студента с |")
            print("|---:|:---|:---|:---|---:|:---:|:---:|")
            for i, item in enumerate(answered, start=1):
                print(
                    f"| {i} | <a class=\"student\" href=\"{repo_url(org, item.student)}\">{item.student}</a> | [`{item.branch}`]({item.pr_url}) | "
                    f"{item.review_state} | {item.age} | {fmt_dt(item.waiting_since) or '—'} | {fmt_dt(item.student_after)} |"
                )
            print()

    if no_pr:
        print("## Lab-ветки без PR")
        print()
        print("| Студент | Ветка | CI | Давность | Последний коммит |")
        print("|:---|:---|:---|---:|:---:|")
        for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at):
            print(f"| <a class=\"student\" href=\"{repo_url(org, item.student)}\">{item.student}</a> | `{item.branch}` | {_md_checks(item)} | {item.age} | {fmt_dt(item.updated_at) or '—'} |")
        print()

    print("## Статистика по студентам")
    print()
    print("| № | Студент | Лабы | Ждут | Слито | Откл | Комм. на проверку | Строк +/− | Коммитов | Пушей |")
    print("|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|")
    for i, st in enumerate(sorted(stats, key=_stat_order), start=1):
        labs = ",".join(str(n) for n in sorted(st.lab_numbers)) or "—"
        lines = f"+{st.lines_add}/-{st.lines_del}" if (st.lines_add or st.lines_del) else "—"
        print(
            f"| {i} | <a class=\"student\" href=\"{repo_url(org, st.student)}\">{st.student}</a> | {labs} | {st.open_prs or '—'} | {st.merged or '—'} | {st.declined or '—'} | "
            f"{st.awaiting_commits or '—'} | {lines} | {st.commits_total or '—'} | {st.pushes or '—'} |"
        )
    print()

    in_queue = len(queue)
    print(
        f"**Итого:** студентов {len(stats)} · PR'ов на проверке {in_queue} · "
        f"веток без PR {len(no_pr)} · ждёт проверки коммитов {sum(s.awaiting_commits for s in stats)}"
        + (f" · **репо с ошибками {failed}**" if failed else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", help="сохранить результат в JSON-файл")
    parser.add_argument("--md", action="store_true", help="отчёт в Markdown на stdout (прогресс и ошибки — в stderr)")
    parser.add_argument("--workers", type=int, default=0, help="сколько репозиториев обрабатывать параллельно (0 = все сразу)")
    args = parser.parse_args()

    settings = QueueSettings()
    gh = GitHub(settings.gh_pat)
    teacher = gh.rest.users.get_authenticated().parsed_data.login
    now = datetime.now(UTC)

    students = [r.name for r in iter_org_repos(gh, settings.gh_org) if r.name.startswith(f"{settings.gh_org}-")]
    log = sys.stderr if args.md else sys.stdout
    print(f"Org {settings.gh_org}: {len(students)} student repos\n", file=log)

    cache = json.loads(EVENTS_CACHE_PATH.read_text()) if EVENTS_CACHE_PATH.exists() else {}
    stat_cache = json.loads(STATS_CACHE_PATH.read_text()) if STATS_CACHE_PATH.exists() else {}

    queue: list[QueueItem] = []
    reviewed: list[QueueItem] = []
    no_pr: list[QueueItem] = []
    stats: list[StudentStats] = []
    charts: list[dict] = []
    failed = 0

    workers = args.workers or len(students)

    def process(repo: str, fpool: ThreadPoolExecutor):
        gh = GitHub(settings.gh_pat)
        return collect_student(gh, settings.gh_org, repo, settings.gh_org, now, cache, teacher, stat_cache, fpool)

    with ThreadPoolExecutor(max_workers=workers) as pool, ThreadPoolExecutor(max_workers=48) as fetch_pool:
        futures = {pool.submit(process, repo, fetch_pool): repo for repo in students}
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                q, r, n, st, ch = fut.result()
            except RateLimitExceeded as e:
                failed += 1
                print(f"x {repo:<30} GitHub limit, сброс через {e.retry_after}", file=log)
                continue
            except (RequestFailed, RequestTimeout) as e:
                failed += 1
                print(f"x {repo:<30} {e}", file=log)
                continue
            queue.extend(q)
            reviewed.extend(r)
            no_pr.extend(n)
            stats.append(st)
            charts.extend(ch)

    EVENTS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    STATS_CACHE_PATH.write_text(json.dumps(stat_cache, ensure_ascii=False))
    if args.md:
        report_md(queue, reviewed, no_pr, stats, settings.gh_org, failed, charts)
    else:
        report(queue, reviewed, no_pr, stats, settings.gh_org, failed)

    if args.json:
        payload = {
            "generated_at": now.isoformat(),
            "queue": [asdict(item) for item in sorted(queue, key=lambda it: it.waiting_since or it.updated_at)],
            "reviewed": [asdict(item) for item in sorted(reviewed, key=lambda it: it.waiting_since)],
            "no_pr": [asdict(item) for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at)],
            "students": [asdict(st) for st in stats],
            "labs": charts,
        }
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
        print(f"\nJSON сохранён: {args.json}", file=log)
    raise SystemExit(1 if failed else 0)


if __name__ == "__main__":
    try:
        main()
    except RateLimitExceeded as e:
        print(f"GitHub API limit исчерпан, повторить примерно через {round(e.retry_after.total_seconds() / 60) + 1} мин.", file=sys.stderr)
        raise SystemExit(1) from None
