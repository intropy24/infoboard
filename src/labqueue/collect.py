
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime

from githubkit import GitHub

from .gh import (
    LAB_BRANCH_RE,
    checks_status,
    commit_is_teacher,
    commit_line_stats,
    iter_branches,
    iter_pulls,
    merge_events,
    pushed_at,
    review_summary,
    student_comment_after,
    student_push_count,
    unresolved_threads,
)
from .model import QueueItem, StudentStats, format_age
from .speed import lines_per_hour


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
    b_status: dict[str, str] = {}
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
            if own is not None:
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
    for name, st_ in b_status.items():
        if name not in lab_chart:
            charts.append({"s": student, "b": name, "p": [], "st": st_})
    stats.pushes = student_push_count(repo_events, student, student_shas)
    return queue, reviewed, no_pr, stats, charts
