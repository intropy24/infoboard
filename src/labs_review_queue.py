
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

    def add_points(branch: str, entries: list[tuple[object, str, int, str, int]]) -> None:
        pts = lab_chart.setdefault(branch, {})
        for d, c_sha, a_, msg, dl in entries:
            if not isinstance(d, datetime) or a_ <= 0 or c_sha in pts:
                continue
            pts[c_sha] = (d, a_, msg, dl)

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
    b_status: dict[str, str] = {}  # состояние лабы по ветке для матрицы «прогресс»: merged/reviewed/queue/nopr
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
            if own is not None:  # студенческие коммиты есть, PR нет — статус для матрицы
                b_status[name] = "nopr"
            continue
        if lab_number not in stats.lab_numbers and any(
            p.state == "open" or p.merged_at is not None for p in candidates
        ):
            stats.lab_numbers.append(lab_number)
        for p in candidates:
            if p.state != "open":
                if p.merged_at is not None:
                    stats.merged += 1
                    b_status[name] = "merged"
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
                         commit_line_stats(gh, owner, repo, c.sha, stat_cache)[0], msg_line(c.commit.message),
                         commit_line_stats(gh, owner, repo, c.sha, stat_cache)[1])
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
            add_points(name, [(a, c_sha, line_by_sha[c_sha][0], msgs.get(c_sha, ""), line_by_sha[c_sha][1]) for a, _, c_sha, _ in student_commits])
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
                    b_status[name] = "reviewed"
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
            b_status[name] = "queue"
    charts: list[dict] = []
    for name, pts in lab_chart.items():
        points: list[list] = []
        prev: datetime | None = None
        for c_sha, (d, a_, msg, dl) in sorted(pts.items(), key=lambda e: e[1][0]):
            rate: int | None = None
            if prev is not None and (h := (d - prev).total_seconds() / 3600) > 0:
                rate = max(1, round(a_ / h))
            points.append([int(d.timestamp() * 1000), rate, f"https://github.com/{owner}/{repo}/commit/{c_sha}", c_sha[:7], msg, a_, dl])
            prev = d
        if points:
            charts.append({"s": student, "b": name, "p": points, "st": b_status.get(name, "")})
    for name, st_ in b_status.items():  # ветки без точек: только статус, для матрицы «прогресс»
        if name not in lab_chart:
            charts.append({"s": student, "b": name, "p": [], "st": st_})
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
  var hues = (function () { // цвета студентов: имена сортируются по хэшу и получают
    var h = {}, seen = {}; // равномерно разнесённые оттенки — в старом хэше 20 студентов
    function hs(n) { var v = 0; for (var i = 0; i < n.length; i++) v = (v * 31 + n.charCodeAt(i)) % 2147483647; return v; }
    var names = DATA.map(function (d) { return d.s; }).filter(function (s) { return !seen[s] && (seen[s] = 1); });
    names.sort(function (a, b) { return hs(a) - hs(b); });
    names.forEach(function (s, i) { h[s] = Math.round((i * 360) / names.length) % 360; });
    return h;
  })();
  function hueOf(name) { // цвет студента: шаг 360/N по кругу, повторов нет
    return name in hues ? hues[name] : 0;
  }
  var fmtTick = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });
  var fmtTip = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: '2-digit', hour: '2-digit', minute: '2-digit' });
  var state = { focus: null }; // студент, на чьём имени сейчас курсор списка
  var mode = 'lines'; // 'rate' — стр/ч, 'lines' — строк в коммите (дефолт: строк — шкала честнее)
  function targetAlpha(d) { // цель прозрачности: при наведении кривые студента сплошные, остальные едва видны
    if (d.hidden) return 0.04;
    if (state.focus) return d.student === state.focus ? 0.95 : 0.04;
    return 0.85;
  }
  function col(ctx) { // scriptable-цвет: читает текущее _a — его плавно ведёт rAF-анимация
    var d = ctx.dataset;
    var l = state.focus && d.student === state.focus ? 62 : 55;
    return 'hsla(' + d.hue + ',72%,' + l + '%,' + d._a + ')';
  }
  var ds = DATA.map(function (d) {
    return {
      label: d.s + ' · ' + d.b, student: d.s, branch: d.b, hue: hueOf(d.s), _a: 0.85,
      data: d.p.map(function (pt) { return { x: pt[0], y: pt[5], r: pt[1], l: pt[5], u: pt[2], h: pt[3], m: pt[4] }; }), // дефолт 'lines' → y = строки, setMode перекладывает при переключении
      borderColor: col, backgroundColor: col, pointBackgroundColor: col,
      pointBorderColor: 'transparent', pointBorderWidth: 0,
      pointRadius: 2.5, pointHoverRadius: 4.5, borderWidth: 1.4, tension: 0
    };
  });
  var drag = null; // идёт выделение рамкой: {x0, x1} в пикселях канваса
  var draggedOnce = false; // после перетаскивания подавляем клик, иначе открылся бы коммит
  var cursor = null; // курсор над полем графика: {x, y} — для перекрестия
  var range = { from: null, to: null }; // окно «с/по» в мс — нужно и heat-map
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
        y: { type: 'logarithmic', title: { display: true, text: 'строк в коммите (лог)' },
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
          label: function (ctx) {
            var r = ctx.raw;
            if (mode === 'rate' && r.r === null) return ctx.dataset.label + ': +' + r.l + ' строк — темп не измерим (первый или одновременный коммит)';
            return mode === 'rate' ? ctx.dataset.label + ': +' + r.l + ' строк → ' + ctx.parsed.y + ' стр/ч' : ctx.dataset.label + ': +' + r.l + ' строк';
          },
          afterLabel: function (ctx) { return ctx.raw.m ? '«' + ctx.raw.m + '»' : ''; } } }
      }
    }
  });
  // плавность подсветки: альфа каждой кривой ведётся к цели через rAF (easeOutQuad);
  // шаги ведутся через chart.update('none'), а не chart.render(): Chart.js пересчитывает
  // scriptable-цвета только в update() (configure() сбрасывает кэш опций), а render()
  // рисует старым кэшем — анимация через render() невидима и портила _a для фильтра
  var animId = 0;
  function glide() {
    cancelAnimationFrame(animId);
    var t0 = performance.now();
    var from = ds.map(function (d) { return d._a; });
    var to = ds.map(targetAlpha);
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100), e = k * (2 - k);
      ds.forEach(function (d, i) { d._a = from[i] + (to[i] - from[i]) * e; });
      chart.update('none');
      if (k < 1) animId = requestAnimationFrame(step);
    })(t0);
  }
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
    glide();
    if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); // фильтры действуют и на оверлеи
  }
  (function () {
    var o = document.createElement('option'); o.value = ''; o.textContent = 'все лабы'; selL.appendChild(o);
    labs.forEach(function (v) { var e = document.createElement('option'); e.value = v; e.textContent = v; selL.appendChild(e); });
    selL.onchange = apply;
  })();
  var btns = document.querySelectorAll('#sp-mode button');
  // heat-map «день недели × час»: третья мода вместо кривых рисует сетку 7×24
  // с итоговой строкой снизу (по часам) и колонкой справа (по дням), где цвет
  // ячейки — число коммитов; агрегирует те же серии, что оставили фильтры, в окне «с/по»
  var HINT = 'ячейка — коммиты за час (МСК); наведение на студента — его heatmap';
  var heat = document.getElementById('speed-heat');
  var heatInfo = document.getElementById('sp-heat-info');
  var DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  var HX = 34, HY = 8, HR = 8, HB = 36; // поля под подписи дней и часов
  function textOn(hex, dark) { var L = .299 * parseInt(hex.slice(1, 3), 16) + .587 * parseInt(hex.slice(3, 5), 16) + .114 * parseInt(hex.slice(5, 7), 16); return L > 140 ? (dark ? '#0d1117' : '#1f2328') : '#fff'; } // читаемый текст поверх цвета ячейки
  var heatGeom = null; // {cw, ch} — превращаем пиксели мыши в номер ячейки
  var heatHover = null; // {day, hour} ячейки под курсором (day: 0=вс)
  var heatCur = {}; // целевые клетки — подсказка показывает цель, а не середину твина
  function heatCells() { // {day*24+hour: {n, who}} по коммитам видимых серий в окне «с/по»
    var cells = {};
    ds.forEach(function (d) {
      if (d.hidden) return;
      if (state.focus && d.student !== state.focus) return; // изоляция по наведению — как у кривых
      d.data.forEach(function (p) {
        if (range.from != null && p.x < range.from) return;
        if (range.to != null && p.x > range.to) return;
        var t = new Date(p.x + 108e5); // МСК: +3 ч и читаем UTC-поля (переходов у МСК нет)
        var k = t.getUTCDay() * 24 + t.getUTCHours();
        var c = cells[k] || (cells[k] = { n: 0, who: {} });
        c.n += 1; c.who[d.student] = (c.who[d.student] || 0) + 1;
      });
    });
    return cells;
  }
  var heatShown = {}; // клетки, видимые сейчас — твин интерполирует от них
  var heatAnim = 0; // rAF хендл плавного fade heatmap
  function drawHeat() { // та же плавность, что у кривых: ведём heatmap от heatShown к цели
    var to = heatCur = heatCells();
    cancelAnimationFrame(heatAnim);
    var from = heatShown, t0 = performance.now(), keys = {};
    Object.keys(from).forEach(function (k) { keys[k] = 1; });
    Object.keys(to).forEach(function (k) { keys[k] = 1; });
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100), e = k * (2 - k); // easeOutQuad, те же 100мс, что у glide
      var mid = {};
      Object.keys(keys).forEach(function (kk) {
        var a = (from[kk] && from[kk].n) || 0, b = (to[kk] && to[kk].n) || 0, v = a + (b - a) * e;
        if (v > 0.02) mid[kk] = { n: v, who: (to[kk] || from[kk]).who };
      });
      heatShown = mid;
      heatPaint();
      if (k < 1) heatAnim = requestAnimationFrame(step); else { heatShown = to; heatPaint(); }
    })(t0);
  }
  function heatPaint() {
    var box = heat.parentElement, W = box.clientWidth, H = box.clientHeight;
    if (!W || !H) return;
    var dpr = window.devicePixelRatio || 1;
    if (heat.width !== W * dpr || heat.height !== H * dpr) { heat.width = W * dpr; heat.height = H * dpr; } // под HiDPI, иначе мыло
    var c2 = heat.getContext('2d');
    c2.setTransform(dpr, 0, 0, dpr, 0, 0);
    // цвета — из темы страницы; палитра сетки — каноничные зелёные GitHub-календаря
    var cs = getComputedStyle(document.documentElement);
    var cMut = cs.getPropertyValue('--muted').trim() || '#57606a';
    var rgb = (getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [255, 255, 255]).map(Number);
    var dark = .299 * rgb[0] + .587 * rgb[1] + .114 * rgb[2] < 140; // тёмная тема — по яркости фона
    var PAL = dark ? ['#161b22', '#0e4429', '#006d32', '#26a641', '#39d353']
                   : ['#ebedf0', '#9be9a8', '#40c463', '#30a14e', '#216e39'];
    var cells = heatShown, max = 1;
    Object.keys(cells).forEach(function (k) { max = Math.max(max, cells[k].n); });
    c2.clearRect(0, 0, W, H); // перерисовка поверх старых пикселей оставила бы хвосты подсветки
    function lvOf(n, mx) { if (!n) return 0; var q = Math.sqrt(n / mx); return q > .75 ? 4 : q > .5 ? 3 : q > .25 ? 2 : 1; } // sqrt — редкие часы не тонут
    var GAP = 12, cw = (W - HX - HR - GAP) / 25; // 24 часа + 25-я колонка «итог» по дням; GAP — отступы перед итогами
    var ch = (H - HY - HB - GAP) / 8; // 7 строк дней + 1 итоговая
    var tx = HX + 24 * cw + GAP, ty = HY + 7 * ch + GAP; // левый край итоговой колонки и верх строки
    heatGeom = { cw: cw, ch: ch };
    c2.font = '11px system-ui, sans-serif'; c2.textBaseline = 'middle';
    for (var row = 0; row < 7; row++) {
      var day = (row + 1) % 7; // строки сверху вниз: пн..вс
      c2.fillStyle = cMut; c2.textAlign = 'right';
      c2.fillText(DAYS[row], HX - 6, HY + ch * (row + .5));
      for (var hh = 0; hh < 24; hh++) {
        var x = HX + hh * cw, y = HY + row * ch, cc = cells[day * 24 + hh];
        var n = cc ? cc.n : 0, lv = lvOf(n, max);
        c2.fillStyle = PAL[lv];
        c2.fillRect(x + 1, y + 1, cw - 2, ch - 2);
        if (n) {
          c2.fillStyle = textOn(PAL[lv], dark); c2.textAlign = 'center';
          c2.fillText(String(Math.round(n)), x + cw / 2, y + ch / 2);
        }
      }
    }
    // итоговая строка: распределение коммитов по часу суток за всю неделю; шкала своя,
    // иначе на фоне семикратных сумм строка была бы всегда самого тёмного уровня
    var tot = [], tMax = 1, hh2;
    for (hh2 = 0; hh2 < 24; hh2++) tot[hh2] = 0;
    Object.keys(cells).forEach(function (k) { tot[(+k) % 24] += cells[k].n; });
    for (hh2 = 0; hh2 < 24; hh2++) tMax = Math.max(tMax, tot[hh2]);
    c2.strokeStyle = dark ? 'rgba(139,148,158,.35)' : 'rgba(87,96,106,.3)'; // разделители итоговых строки и колонки
    c2.beginPath();
    c2.moveTo(HX, ty - GAP / 2 + .5); c2.lineTo(tx - GAP / 2, ty - GAP / 2 + .5);
    c2.moveTo(tx - GAP / 2 + .5, HY); c2.lineTo(tx - GAP / 2 + .5, ty + ch);
    c2.stroke();
    c2.fillStyle = cMut; c2.textAlign = 'right';
    c2.fillText('итог', HX - 6, ty + ch / 2);
    for (hh2 = 0; hh2 < 24; hh2++) {
      var nx = HX + hh2 * cw, nT = tot[hh2], lvT = lvOf(nT, tMax);
      c2.fillStyle = PAL[lvT];
      c2.fillRect(nx + 1, ty + 1, cw - 2, ch - 2);
      if (nT) {
        c2.fillStyle = textOn(PAL[lvT], dark); c2.textAlign = 'center';
        c2.fillText(String(Math.round(nT)), nx + cw / 2, ty + ch / 2);
      }
    }
    // итоговая колонка: сумма коммитов по каждому дню недели; шкала своя, как у строки
    var dtot = [], dMax = 1, all = 0, d2;
    Object.keys(cells).forEach(function (k) {
      var dn = Math.floor((+k) / 24), t = cells[k].n;
      dtot[dn] = (dtot[dn] || 0) + t; all += t;
    });
    for (d2 = 0; d2 < 7; d2++) { dtot[d2] = dtot[d2] || 0; dMax = Math.max(dMax, dtot[d2]); }
    for (d2 = 0; d2 < 7; d2++) { // порядок строк тот же (пн..вс), та же палитра и текст поверх
      var dY = HY + d2 * ch, nD = dtot[(d2 + 1) % 7], lvD = lvOf(nD, dMax);
      c2.fillStyle = PAL[lvD]; c2.fillRect(tx + 1, dY + 1, cw - 2, ch - 2);
      if (nD) {
        c2.fillStyle = textOn(PAL[lvD], dark); c2.textAlign = 'center';
        c2.fillText(String(Math.round(nD)), tx + cw / 2, dY + ch / 2);
      }
    }
    // угол сетки: grand total — muted-цифра на пустой клетке (свой уровень не выдумываем)
    c2.fillStyle = PAL[0]; c2.fillRect(tx + 1, ty + 1, cw - 2, ch - 2);
    c2.fillStyle = cMut; c2.textAlign = 'center';
    if (all) c2.fillText(String(Math.round(all)), tx + cw / 2, ty + ch / 2);
    c2.fillText('итог', tx + cw / 2, H - HB + 8); // подпись колонки — в полосе подписей часов
    c2.fillStyle = cMut; c2.textAlign = 'center';
    for (var h2 = 0; h2 < 24; h2 += 2) c2.fillText(String(h2), HX + h2 * cw + cw / 2, H - HB + 8);
    if (heatHover) { // подсветка ячейки под курсором
      var hx = HX + heatHover.hour * cw, hy = HY + ((heatHover.day + 6) % 7) * ch;
      c2.strokeStyle = '#4493f8'; c2.lineWidth = 2;
      c2.strokeRect(hx + 1.5, hy + 1.5, cw - 3, ch - 3);
    }
  }
  heat.addEventListener('mousemove', function (e) {
    if (!heatGeom) return;
    var r = heat.getBoundingClientRect();
    var hh = Math.floor((e.clientX - r.left - HX) / heatGeom.cw);
    var row = Math.floor((e.clientY - r.top - HY) / heatGeom.ch);
    var hov = (hh >= 0 && hh < 24 && row >= 0 && row < 7) ? { hour: hh, day: (row + 1) % 7 } : null;
    var same = !!(hov && heatHover && hov.day === heatHover.day && hov.hour === heatHover.hour);
    if (!same && (hov || heatHover)) { heatHover = hov; heatPaint(); } // перерисовка только при смене ячейки
    if (!hov) { heatInfo.textContent = HINT; return; }
    var cc = heatCur[hov.day * 24 + hov.hour];
    var span = DAYS[(hov.day + 6) % 7] + ' ' + hov.hour + ':00–' + ((hov.hour + 1) % 24) + ':00';
    if (!cc) { heatInfo.textContent = span + ' — пусто'; return; }
    var who = Object.keys(cc.who);
    heatInfo.textContent = span + ' — ' + cc.n + ' комм.: ' + who.slice(0, 4).join(', ') + (who.length > 4 ? ' +' + (who.length - 4) : '');
  });
  heat.addEventListener('mouseleave', function () { heatHover = null; if (mode === 'heat') { heatPaint(); heatInfo.textContent = HINT; } });
  // переключение темы (🌓 меняет data-theme на <html>) — перекрашиваем heatmap на лету
  if (window.MutationObserver) new MutationObserver(function () { if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  // матрица «прогресс»: студенты по вертикали, лабы по горизонтали; цвет ячейки —
  // статус лабы (st из данных), число — коммиты; подсветка строк/столбцов твином 100мс,
  // уважает пик (клик-список) и лабу (селект); окно «с/по» на матрицу не влияет
  var mxCv = document.getElementById('speed-mx');
  var mxInfo = document.getElementById('sp-mx-info');
  var MHINT = 'строка — студент, столбец — лаба; число — коммиты; клик по ячейке — последний коммит';
  var MXCOL = { merged: '#3fb950', queue: '#d29922', reviewed: '#58a6ff', nopr: '#8b949e' }; // поярче — акценты GitHub dark
  var MXTXT = { merged: 'влита', queue: 'на проверке', reviewed: 'проверено', nopr: 'нет PR' };
  var mxCell = {}, mxNames = {}, mLabs = {};
  DATA.forEach(function (d) { // студент|ветка -> ячейка; пустые серии несут только статус
    var n = d.p.length, add = 0, dl = 0;
    d.p.forEach(function (pt) { add += pt[5]; dl += pt[6] || 0; });
    mxCell[d.s + '|' + d.b] = { st: d.st || '', n: n, add: add, dl: dl, u: n ? d.p[n - 1][2] : '' };
    mxNames[d.s] = 1; mLabs[d.b] = 1;
  });
  var mxColL = Object.keys(mLabs).sort();
  var mxRowN = Object.keys(mxNames).sort(function (a, b) { // успевающие вверху: по числу затронутых лаб, далее по имени
    var la = 0, lb = 0;
    mxColL.forEach(function (l) { if (mxCell[a + '|' + l]) la++; if (mxCell[b + '|' + l]) lb++; });
    return lb - la || (a < b ? -1 : 1);
  });
  var mxRowShown = {}, mxCvShown = {}, mxAnim = 0, mxGeom = null, mxHover = null;
  function mxRowT(s) { // подсветка строки: наведение на имя сильнее, чем пик
    var any = Object.keys(picked).length > 0;
    if (state.focus) return state.focus === s ? 1 : .12;
    return !any || picked[s] ? 1 : .12;
  }
  function mxCvT(b) { return !selL.value || b === selL.value ? 1 : .15; }
  function drawMatrix() { // те же 100мс easeOutQuad, но ведём прозрачность строк и столбцов
    cancelAnimationFrame(mxAnim);
    var rF = {}, rT = {}, cF = {}, cT = {}, t0 = performance.now();
    mxRowN.forEach(function (s) { rF[s] = s in mxRowShown ? mxRowShown[s] : mxRowT(s); rT[s] = mxRowT(s); });
    mxColL.forEach(function (b) { cF[b] = b in mxCvShown ? mxCvShown[b] : mxCvT(b); cT[b] = mxCvT(b); });
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100), e = k * (2 - k), mr = {}, mc = {};
      mxRowN.forEach(function (s) { mr[s] = rF[s] + (rT[s] - rF[s]) * e; });
      mxColL.forEach(function (b) { mc[b] = cF[b] + (cT[b] - cF[b]) * e; });
      mxRowShown = mr; mxCvShown = mc;
      matrixPaint();
      if (k < 1) mxAnim = requestAnimationFrame(step);
    })(t0);
  }
  function matrixPaint() {
    var box = mxCv.parentElement, W = box.clientWidth, H = box.clientHeight;
    if (!W || !H) return;
    var dpr = window.devicePixelRatio || 1;
    if (mxCv.width !== W * dpr || mxCv.height !== H * dpr) { mxCv.width = W * dpr; mxCv.height = H * dpr; }
    var c2 = mxCv.getContext('2d');
    c2.setTransform(dpr, 0, 0, dpr, 0, 0);
    c2.clearRect(0, 0, W, H);
    var cs = getComputedStyle(document.documentElement);
    var cMut = cs.getPropertyValue('--muted').trim() || '#57606a';
    var rgb = (getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [255, 255, 255]).map(Number);
    var dark = .299 * rgb[0] + .587 * rgb[1] + .114 * rgb[2] < 140;
    var empty = dark ? '#161b22' : '#ebedf0', n = mxRowN.length, m = mxColL.length, TOP = 24, BOT = 36, i;
    c2.font = '11px system-ui, sans-serif'; c2.textBaseline = 'middle';
    var LW = 96;
    for (i = 0; i < n; i++) LW = Math.max(LW, Math.min(150, c2.measureText(mxRowN[i]).width + 14));
    var cw = (W - LW - 8) / m, rh = (H - TOP - BOT) / n;
    mxGeom = { LW: LW, cw: cw, rh: rh, TOP: TOP };
    mxInfo.style.left = LW + 6 + 'px'; // инфо-строка начинается правее имён, как и сетка
    c2.fillStyle = cMut; c2.textAlign = 'center';
    for (i = 0; i < m; i++) c2.fillText(cw > 64 ? mxColL[i] : mxColL[i].replace('lab-', 'л'), LW + cw * (i + .5), TOP / 2);
    for (var r = 0; r < n; r++) {
      var s = mxRowN[r], ra = s in mxRowShown ? mxRowShown[s] : 1;
      c2.globalAlpha = ra; c2.fillStyle = cMut; c2.textAlign = 'right';
      var nm = s;
      while (nm.length > 2 && c2.measureText(nm + '…').width > LW - 12) nm = nm.slice(0, -1);
      c2.fillText(nm === s ? s : nm + '…', LW - 6, TOP + rh * (r + .5));
      for (var c = 0; c < m; c++) {
        var cell = mxCell[s + '|' + mxColL[c]], st = cell ? cell.st : '';
        c2.globalAlpha = ra * ((mxColL[c] in mxCvShown) ? mxCvShown[mxColL[c]] : 1);
        c2.fillStyle = cell && st ? MXCOL[st] || empty : empty;
        c2.fillRect(LW + cw * c + 1, TOP + rh * r + 1, cw - 2, rh - 2);
        if (cell && cell.n) {
          c2.fillStyle = textOn(MXCOL[st] || empty, dark); c2.textAlign = 'center';
          var txt = cell.add || cell.dl ? cell.n + ' (+' + cell.add + '/−' + cell.dl + ')' : String(cell.n);
          if (c2.measureText(txt).width > cw - 8) txt = String(cell.n); // если не влезает — оставляем число коммитов
          c2.fillText(txt, LW + cw * (c + .5), TOP + rh * (r + .5));
        }
      }
    }
    c2.globalAlpha = 1;
    var lx = LW, ly = H - BOT + 8; // легенда статусов: над инфо-строкой (BOT/2 под ней занят подсказкой)
    ['merged', 'queue', 'reviewed', 'nopr'].forEach(function (st) {
      c2.fillStyle = MXCOL[st]; c2.fillRect(lx, ly - 5, 10, 10);
      c2.fillStyle = cMut; c2.textAlign = 'left'; c2.font = '10px system-ui, sans-serif';
      c2.fillText(MXTXT[st], lx + 14, ly + 1);
      lx += 20 + c2.measureText(MXTXT[st]).width;
    });
    if (mxHover) {
      c2.strokeStyle = '#4493f8'; c2.lineWidth = 2;
      c2.strokeRect(LW + mxHover.c * cw + 1.5, TOP + mxHover.r * rh + 1.5, cw - 3, rh - 3);
    }
  }
  mxCv.addEventListener('mousemove', function (e) {
    if (!mxGeom) return;
    var rct = mxCv.getBoundingClientRect();
    var c = Math.floor((e.clientX - rct.left - mxGeom.LW) / mxGeom.cw);
    var r = Math.floor((e.clientY - rct.top - mxGeom.TOP) / mxGeom.rh);
    var hov = c >= 0 && c < mxColL.length && r >= 0 && r < mxRowN.length ? { r: r, c: c } : null;
    var same = !!(hov && mxHover && hov.r === mxHover.r && hov.c === mxHover.c);
    if (!same && (hov || mxHover)) { mxHover = hov; matrixPaint(); }
    if (!hov) { mxInfo.textContent = MHINT; return; }
    var s2 = mxRowN[hov.r], b = mxColL[hov.c], cell = mxCell[s2 + '|' + b];
    if (!cell) { mxInfo.textContent = s2 + ' · ' + b + ' — ещё не брался'; return; }
    mxInfo.textContent = s2 + ' · ' + b + ' — ' + (MXTXT[cell.st] || 'статус не определён') + (cell.n ? ', ' + cell.n + ' комм.' : '') + (cell.add || cell.dl ? ', +' + cell.add + '/−' + cell.dl : '');
  });
  mxCv.addEventListener('mouseleave', function () { mxHover = null; if (mode === 'matrix') { matrixPaint(); mxInfo.textContent = MHINT; } });
  mxCv.addEventListener('click', function () { // клик по ячейке — последний коммит лабы этого студента
    if (!mxHover) return;
    var cell = mxCell[mxRowN[mxHover.r] + '|' + mxColL[mxHover.c]];
    if (cell && cell.u) window.open(cell.u, '_blank');
  });
  function setMode(m) { // переключение метрики Y / оверлеев: все величины уже лежат в точках
    mode = m;
    var heatOn = m === 'heat', mxOn = m === 'matrix';
    chart.canvas.style.display = heatOn || mxOn ? 'none' : '';
    heat.style.display = heatOn ? 'block' : 'none';
    heatInfo.style.display = heatOn ? 'block' : 'none';
    mxCv.style.display = mxOn ? 'block' : 'none';
    mxInfo.style.display = mxOn ? 'block' : 'none';
    Array.prototype.forEach.call(btns, function (b) { b.classList.toggle('on', b.getAttribute('data-mode') === m); });
    if (heatOn) { drawHeat(); return; }
    if (mxOn) { drawMatrix(); return; }
    ds.forEach(function (d) { d.data.forEach(function (p) { p.y = m === 'rate' ? (p.r === null ? 1 : p.r) : p.l; }); });
    chart.options.scales.y.title.text = m === 'rate' ? 'скорость коммита, стр/ч (лог)' : 'строк в коммите (лог)';
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
  function applyTime(from, to) { // мс или null — границы окна по X (и для heat-map)
    range.from = from == null ? null : from; range.to = to == null ? null : to;
    chart.options.scales.x.min = from == null ? undefined : from;
    chart.options.scales.x.max = to == null ? undefined : to;
    inpF.value = toInput(from); inpT.value = toInput(to);
    chart.update('none');
    if (mode === 'heat') drawHeat();
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
  // 📅: штатный нативный выбор даты живёт в невидимом datetime-local под текстовым
  // полем; по клику открываем его picker, результат форматируем в наш дд.мм формат
  function toLocalISO(ms) {
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }
  Array.prototype.forEach.call(document.querySelectorAll('.sp-cal'), function (b) {
    var tx = document.getElementById(b.getAttribute('data-for'));
    var dt = tx.parentElement.querySelector('input[type="datetime-local"]');
    b.onclick = function () {
      var ms = parseInput(tx.value);
      dt.value = ms === null || isNaN(ms) ? '' : toLocalISO(ms); // предзаполним, если уже вписано руками
      try { dt.showPicker(); } catch (e) { dt.focus(); } // где showPicker нет — хотя бы фокус
    };
    dt.onchange = function () {
      tx.value = dt.value ? toInput(new Date(dt.value).getTime()) : '';
      tx.classList.remove('bad');
      onInputTime();
    };
  });
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
    li.onmouseenter = function () { state.focus = s; glide(); if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); };
    li.onmouseleave = function () { state.focus = null; glide(); if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); };
    li.onclick = function () { // как чекбокс: один клик — видеть только его, ещё клик — добавить ещё одного
      if (picked[s]) { delete picked[s]; li.classList.remove('on'); }
      else { picked[s] = true; li.classList.add('on'); }
      apply();
    };
  });
})();
"""


def _speed_chart(charts: list[dict]) -> None:
    data = [c for c in charts if c["p"] or c.get("st")]
    if not data:
        return
    print('<div id="speed-wrap">')
    print('<div id="speed-side">')
    print('<div id="speed-controls"><select id="sp-lab"></select>')
    print('<div id="sp-mode"><button data-mode="rate">стр/ч</button><button data-mode="lines" class="on">строк в коммите</button><button data-mode="heat">день×час</button><button data-mode="matrix">прогресс</button></div>')
    print('<div id="sp-times">')
    print('<label class="sp-dt"><input id="sp-from" type="text" placeholder="с: дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"><input type="datetime-local" tabindex="-1" aria-hidden="true"><button type="button" class="sp-cal" data-for="sp-from" title="выбрать дату">📅</button></label>')
    print('<label class="sp-dt"><input id="sp-to" type="text" placeholder="по: дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"><input type="datetime-local" tabindex="-1" aria-hidden="true"><button type="button" class="sp-cal" data-for="sp-to" title="выбрать дату">📅</button></label>')
    print('<button id="sp-reset" title="показать всё время">×</button>')
    print('</div>')
    print('</div>')
    print('<ul id="sp-list">')
    for s in sorted({d["s"] for d in data}):
        print(f'<li data-student="{s}"><i></i>{s}</li>')
    print('</ul>')
    print('</div>')
    print('<div id="speed-box"><canvas id="speed-canvas"></canvas><canvas id="speed-heat"></canvas><canvas id="speed-mx"></canvas><div id="sp-heat-info">ячейка — коммиты за час (МСК); наведение на студента — его heatmap</div><div id="sp-mx-info">строка — студент, столбец — лаба; число — коммиты; клик по ячейке — последний коммит</div></div>')
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
    print()
    _speed_chart(charts)


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
