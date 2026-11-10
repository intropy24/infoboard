
import argparse
import json
import sys
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import UTC, datetime

from githubkit import GitHub
from githubkit.exception import RateLimitExceeded, RequestFailed, RequestTimeout

from . import __doc__ as PKG_DOC
from .collect import collect_student
from .config import EVENTS_CACHE_PATH, STATS_CACHE_PATH
from .model import QueueItem, StudentStats
from .report import report, report_md


def main(settings_cls: type, iter_repos: Callable) -> None:
    parser = argparse.ArgumentParser(description=PKG_DOC)
    parser.add_argument("--json", help="сохранить результат в JSON-файл")
    parser.add_argument("--md", action="store_true", help="отчёт в Markdown на stdout (прогресс и ошибки — в stderr)")
    parser.add_argument("--workers", type=int, default=0, help="сколько репозиториев обрабатывать параллельно (0 = все сразу)")
    args = parser.parse_args()

    settings = settings_cls()
    gh = GitHub(settings.gh_pat)
    teacher = gh.rest.users.get_authenticated().parsed_data.login
    now = datetime.now(UTC)

    students = [r.name for r in iter_repos(gh, settings.gh_org) if r.name.startswith(f"{settings.gh_org}-")]
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
