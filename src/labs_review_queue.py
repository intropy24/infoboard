import argparse
import json
import re
import sys
from collections.abc import Iterator
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from itertools import pairwise
from pathlib import Path

from githubkit import GitHub
from githubkit.exception import RateLimitExceeded, RequestFailed, RequestTimeout
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

LAB_BRANCH_RE = re.compile(r"^lab-[0-9]{2}([-.].*)?$")

EVENTS_CACHE_PATH = Path(__file__).resolve().parent / "events-cache.json"
STATS_CACHE_PATH = Path(__file__).resolve().parent / "commit-stats-cache.json"

REVIEW_MARKS = {
    "APPROVED": "✅ approved",
    "CHANGES_REQUESTED": "🔄 changes",
    "COMMENTED": "💬 commented",
    "DISMISSED": "🚫 dismissed",
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    gh_pat: str = Field(..., alias="GH_PAT")


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


def fmt_dt(iso: str) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(UTC).strftime("%d.%m.%Y %H:%M")


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
) -> tuple[list[QueueItem], list[QueueItem], list[QueueItem], StudentStats]:
    student = repo.removeprefix(f"{prefix}-")
    stats = StudentStats(student=student)
    student_shas: set[str] = set()
    stat_shas: set[str] = set()

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
    for name, sha in branches:
        if name in ("main", "master") or not LAB_BRANCH_RE.match(name):
            continue
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
                waiting_since=date.isoformat(),
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
            ))
    stats.pushes = student_push_count(repo_events, student, student_shas)
    return queue, reviewed, no_pr, stats


def _pad(s: str, width: int) -> str:
    w = sum(
        2 if 0x2300 <= ord(c) <= 0x27BF or 0x2B00 <= ord(c) <= 0x2BFF or 0x1F000 <= ord(c) <= 0x1FAFF else 1
        for c in s
    )
    return s + " " * max(0, width - w)


def report(
    queue: list[QueueItem], reviewed: list[QueueItem], no_pr: list[QueueItem], stats: list[StudentStats], org: str, failed: int
) -> None:
    queue.sort(key=lambda item: item.waiting_since or item.updated_at)
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
            f"{_pad(f'{item.review_state}{draft}', 16)} {since}{speed}"
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
            print("\n=== Препод ответил, студент отделался комментарием без правок ===")
            for i, item in enumerate(answered, start=1):
                print(
                    f"{i:>3}. 💬 {item.age:<8} {item.student:<18} {item.branch:<26} "
                    f"{_pad(item.review_state, 16)}— ревью с {item.waiting_since[:10]}, ответ студента с {item.student_after[:10]}"
                )

    if no_pr:
        print("\n=== Lab-ветки без PR ===")
        for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at):
            print(f"      ⏳ {item.age:<8} {item.student:<18} {item.branch} (последний коммит {item.updated_at[:10]})")

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


def report_md(
    queue: list[QueueItem], reviewed: list[QueueItem], no_pr: list[QueueItem], stats: list[StudentStats], org: str, failed: int
) -> None:
    queue.sort(key=lambda item: item.waiting_since or item.updated_at)
    print(f"# Очередь лабораторных — {org}")
    print()
    print(f"_Обновлено: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC_")
    print()
    print("## Очередь на проверку")
    print()
    if not queue:
        print("Пусто — все PR'ы проверены ✅")
    else:
        print("| № | Студент | Ветка | Коммитов (ждут/всего) | +/− | Ревью | Ждёт | С (UTC) | ⚡ стр/ч (ср/макс·инт) |")
        print("|---:|:---|:---|---:|---:|:---|---:|:---:|:---:|")
        for i, item in enumerate(queue, start=1):
            draft = " 🚧" if item.draft else ""
            plus_minus = f"+{item.additions}/-{item.deletions}" if item.additions is not None else "—"
            speed = f"{item.speed[0]}/{item.speed[1]} ({item.speed[2]})" if item.speed else "—"
            comm = f"{item.commits}/{item.commits_all}" if item.commits_all is not None else (item.commits if item.commits is not None else "—")
            print(
                f"| {i} | {item.student} | [`{item.branch}`]({item.pr_url}) | "
                f"{comm} | {plus_minus} | {item.review_state}{draft} | "
                f"{item.age} | {fmt_dt(item.waiting_since) or '—'} | {speed} |"
            )
    print()

    if reviewed:
        silent = sorted((it for it in reviewed if not it.student_after), key=lambda it: it.waiting_since)
        answered = sorted((it for it in reviewed if it.student_after), key=lambda it: it.waiting_since)
        if silent:
            print("## Ожидаются исправления")
            print()
            print("| № | Студент | Ветка | Ревью | convs | Давность | С (UTC) |")
            print("|---:|:---|:---|:---|---:|---:|:---:|")
            for i, item in enumerate(silent, start=1):
                print(
                    f"| {i} | {item.student} | [`{item.branch}`]({item.pr_url}) | "
                    f"{item.review_state} | {item.unresolved if item.unresolved is not None else '—'} | {item.age} | {fmt_dt(item.waiting_since) or '—'} |"
                )
            print()
        if answered:
            print("## Препод ответил, студент отделался комментарием без правок")
            print()
            print("| № | Студент | Ветка | Ревью | Давность | Ревью с | Ответ студента с |")
            print("|---:|:---|:---|:---|---:|:---:|:---:|")
            for i, item in enumerate(answered, start=1):
                print(
                    f"| {i} | {item.student} | [`{item.branch}`]({item.pr_url}) | "
                    f"{item.review_state} | {item.age} | {item.waiting_since[:10] or '—'} | {item.student_after[:10]} |"
                )
            print()

    if no_pr:
        print("## Lab-ветки без PR")
        print()
        print("| Студент | Ветка | Давность | Последний коммит |")
        print("|:---|:---|---:|:---:|")
        for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at):
            print(f"| {item.student} | `{item.branch}` | {item.age} | {item.updated_at[:10] or '—'} |")
        print()

    print("## Статистика по студентам")
    print()
    print("| № | Студент | Лабы | Ждут | Слито | Откл | Комм. на проверку | Строк +/− | Коммитов | Пушей |")
    print("|---:|:---|:---|---:|---:|---:|---:|---:|---:|---:|")
    for i, st in enumerate(sorted(stats, key=_stat_order), start=1):
        labs = ",".join(str(n) for n in sorted(st.lab_numbers)) or "—"
        lines = f"+{st.lines_add}/-{st.lines_del}" if (st.lines_add or st.lines_del) else "—"
        print(
            f"| {i} | {st.student} | {labs} | {st.open_prs or '—'} | {st.merged or '—'} | {st.declined or '—'} | "
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

    settings = QueueSettings()  # type: ignore
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
                q, r, n, st = fut.result()
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

    EVENTS_CACHE_PATH.write_text(json.dumps(cache, ensure_ascii=False))
    STATS_CACHE_PATH.write_text(json.dumps(stat_cache, ensure_ascii=False))
    (report_md if args.md else report)(queue, reviewed, no_pr, stats, settings.gh_org, failed)

    if args.json:
        payload = {
            "generated_at": now.isoformat(),
            "queue": [asdict(item) for item in sorted(queue, key=lambda it: it.waiting_since or it.updated_at)],
            "reviewed": [asdict(item) for item in sorted(reviewed, key=lambda it: it.waiting_since)],
            "no_pr": [asdict(item) for item in sorted(no_pr, key=lambda it: it.waiting_since or it.updated_at)],
            "students": [asdict(st) for st in stats],
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
