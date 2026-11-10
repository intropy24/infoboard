
from datetime import datetime

from .config import MSK
from .jplag_chart import jplag_section
from .model import QueueItem, StudentStats
from .speed import speed_chart


def checks_rank(item: QueueItem) -> int:
    c = item.checks
    if c.startswith("✅"):
        return 0
    if c.startswith("⏳") or c == "—":
        return 1
    return 2


def fmt_dt(iso: str) -> str:
    if not iso:
        return ""
    return datetime.fromisoformat(iso).astimezone(MSK).strftime("%d.%m.%Y %H:%M")


def repo_url(org: str, student: str) -> str:
    return f"https://github.com/{org}/{org}-{student}"


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
    jplag_section()
    speed_chart(charts)
