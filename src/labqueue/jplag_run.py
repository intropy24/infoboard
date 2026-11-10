
import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

import httpx
from githubkit import GitHub
from githubkit.exception import RequestFailed, RequestTimeout

from .config import JPLAG_SUMMARY_PATH, MSK

JPLAG_VERSION = "6.3.0"
JPLAG_JAR_URL = (
    f"https://github.com/jplag/JPlag/releases/download/v{JPLAG_VERSION}"
    f"/jplag-{JPLAG_VERSION}-jar-with-dependencies.jar"
)
JPLAG_JAR_SHA256 = "5f2c21e8b88ed77134effcb3a5a3ab13d188f6a3e16d401387f7479e92db9aa2"

HERE = Path(__file__).resolve().parent.parent
TOOLS_DIR = HERE / "tools"
SRC_CACHE_DIR = HERE / "jplag-src"
OUT_DIR = HERE / "jplag-reports"

LAB_BRANCH_RE = re.compile(r"^lab-(\d{2})([-.].*)?$")
TASK_PREFIX_RE = re.compile(r"^(\d{2})[-_]")
TASK_CONTAINERS = frozenset({"01-kids-labs"})
NOISE_DIRS = frozenset({".venv", "venv", "site-packages", "__pycache__", "node_modules", ".git"})
MAX_FILE_BYTES = 300_000
MAX_PAIRS_IN_SUMMARY = 400
MAX_SHOWN_IN_SUMMARY = 60


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def task_group(rel: Path) -> str:
    for part in reversed(rel.parts[:-1]):
        if part in TASK_CONTAINERS:
            break
        m = TASK_PREFIX_RE.match(part)
        if m:
            return f"task-{m.group(1)}"
    m = TASK_PREFIX_RE.match(rel.name)
    if m:
        return f"task-{m.group(1)}"
    return "project"


def group_sort(g: str) -> tuple[int, str]:
    m = re.match(r"task-(\d{2})", g)
    return (int(m.group(1)) if m else 99, g)


def ensure_jar() -> Path:
    jar = TOOLS_DIR / f"jplag-{JPLAG_VERSION}-jar-with-dependencies.jar"
    if jar.exists() and hashlib.sha256(jar.read_bytes()).hexdigest() == JPLAG_JAR_SHA256:
        return jar
    print(f"скачиваю JPlag {JPLAG_VERSION} (~80 МБ) …", flush=True)
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(JPLAG_JAR_URL, headers={"User-Agent": "jplag_check.py"})
    tmp = jar.with_suffix(".part")
    with urllib.request.urlopen(req) as r, open(tmp, "wb") as f:
        shutil.copyfileobj(r, f, 1024 * 1024)
    digest = hashlib.sha256(tmp.read_bytes()).hexdigest()
    if digest != JPLAG_JAR_SHA256:
        tmp.unlink(missing_ok=True)
        raise SystemExit(f"sha256 jar не совпал с ожидаемым ({digest}) — проверьте URL/версию")
    tmp.rename(jar)
    return jar


def report_pairs(result: Path, label: str, n_subs: int, min_sim: float) -> tuple[list[dict], list[dict]]:
    with zipfile.ZipFile(result) as z:
        names = set(z.namelist())
        top = json.loads(z.read("topComparisons.json"))
        pairs: list[dict] = []
        all_pairs: list[dict] = []
        for cmp_ in top:
            a, b = cmp_["firstSubmission"], cmp_["secondSubmission"]
            sims = cmp_.get("similarities") or {}
            avg = float(sims.get("AVG") or 0.0) * 100
            mx = float(sims.get("MAX") or 0.0) * 100
            all_pairs.append({"a": a, "b": b, "avg": round(avg, 1), "max": round(mx, 1)})
            if avg < min_sim:
                continue
            files: list[str] = []
            seen: set[tuple[str, str]] = set()
            for cand in (f"comparisons/{a}-{b}.json", f"comparisons/{b}-{a}.json"):
                if cand in names:
                    for m in json.loads(z.read(cand)).get("matches") or []:
                        f1 = Path(str(m.get("firstFileName", "?"))).name
                        f2 = Path(str(m.get("secondFileName", "?"))).name
                        if (f1, f2) not in seen:
                            seen.add((f1, f2))
                            files.append(f"{f1} <-> {f2}")
                    break
            pairs.append({"a": a, "b": b, "avg": round(avg, 1), "max": round(mx, 1), "files": files[:3]})
    pairs.sort(key=lambda p: -p["avg"])
    all_pairs.sort(key=lambda p: -p["avg"])
    if not pairs:
        print(f"  {label:<20} {n_subs:>2} работы — чисто (порог {min_sim:g}%)")
        return pairs, all_pairs
    print(f"  {label:<20} {n_subs:>2} работы, пар >= {min_sim:g}%: {len(pairs)} — смотреть: jplag_check.py --view {label}")
    for p in pairs[:10]:
        print(f"     {p['avg']:5.1f}% (max {p['max']:4.0f}%)  {p['a']} <-> {p['b']}" + (f"  — {', '.join(p['files'])}" if p["files"] else ""))
    if len(pairs) > 10:
        print(f"     … и ещё {len(pairs) - 10} — в отчёте")
    return pairs, all_pairs


def run_jplag(
    pat: str,
    org: str,
    students: list[str],
    *,
    labs: list[int] | None = None,
    by_task: bool = False,
    min_sim: float = 80.0,
    workers: int = 8,
    force: bool = False,
    write_json: bool = False,
) -> int:
    wanted_labs = set(labs) if labs else None

    def student_files(repo: str, fgh: GitHub) -> list[tuple[int, str, str, Path, Path]]:
        student = repo.removeprefix(f"{org}-")
        out: list[tuple[int, str, str, Path, Path]] = []
        branches = []
        for page in (1, 2):
            batch = fgh.rest.repos.list_branches(org, repo, per_page=100, page=page).parsed_data
            branches += batch
            if len(batch) < 100:
                break
        candidates: dict[int, list[tuple[str, str]]] = {}
        for b in branches:
            m = LAB_BRANCH_RE.match(b.name)
            if not m:
                continue
            lab = int(m.group(1))
            if wanted_labs and lab not in wanted_labs:
                continue
            candidates.setdefault(lab, []).append((b.name, b.commit.sha))
        best: dict[int, str] = {}
        for lab, cands in candidates.items():
            if len(cands) == 1:
                best[lab] = cands[0][1]
            else:
                def head_date(sha: str) -> datetime:
                    c = fgh.rest.repos.get_commit(org, repo, sha).parsed_data.commit
                    d = c.committer.date if c.committer else None
                    return d if isinstance(d, datetime) else datetime.min.replace(tzinfo=UTC)

                best[lab] = max(cands, key=lambda ns: head_date(ns[1]))[1]
        if not best:
            return out
        tree = fgh.rest.git.get_tree(org, repo, "main", recursive="1").parsed_data
        if tree.truncated:
            print(f"! {repo}: дерево main обрезано — исключение шаблона может быть неполным", file=sys.stderr)
        template = {t.path: t.sha for t in tree.tree if t.type == "blob"}

        for lab, sha in sorted(best.items()):
            dest = SRC_CACHE_DIR / f"{repo}@{sha}"
            if force and dest.exists():
                shutil.rmtree(dest)
            if not dest.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                r = httpx.get(
                    f"https://api.github.com/repos/{org}/{repo}/tarball/{sha}",
                    headers={"Authorization": f"token {pat}", "User-Agent": "jplag_check.py"},
                    follow_redirects=True,
                    timeout=120,
                )
                r.raise_for_status()
                tmp = dest.with_name(dest.name + ".tmp")
                shutil.rmtree(tmp, ignore_errors=True)
                with tarfile.open(fileobj=io.BytesIO(r.content)) as tar:
                    tar.extractall(tmp, [m for m in tar.getmembers() if m.isfile()], filter="data")
                inner = next(p for p in tmp.iterdir() if p.is_dir())
                for p in list(inner.iterdir()):
                    p.rename(tmp / p.name)
                inner.rmdir()
                tmp.rename(dest)
            for f in sorted(dest.rglob("*.py")):
                rel = f.relative_to(dest)
                if any(part in NOISE_DIRS for part in rel.parts):
                    continue
                data = f.read_bytes()
                if len(data) == 0 or len(data) > MAX_FILE_BYTES:
                    continue
                if template.get(str(rel)) == blob_sha(data):
                    continue
                out.append((lab, task_group(rel), student, f, rel))
        return out

    submissions: dict[int, dict[str, dict[str, list[tuple[Path, Path]]]]] = {}
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(student_files, repo, GitHub(pat)): repo for repo in students}
        for fut in as_completed(futures):
            repo = futures[fut]
            try:
                items = fut.result()
            except Exception as e:
                failed += 1
                print(f"x {repo:<30} {e}", file=sys.stderr)
                continue
            for lab, group, student, path, rel in items:
                submissions.setdefault(lab, {}).setdefault(group, {}).setdefault(student, []).append((path, rel))

    if not submissions:
        raise SystemExit("нет ни одной lab-ветки со студенческими *.py — сравнивать нечего")

    jar = ensure_jar()
    java = shutil.which("java")
    if not java:
        raise SystemExit("нужна Java 25+ (java не найдена в PATH)")
    src_run = OUT_DIR / "_src"
    if src_run.exists():
        shutil.rmtree(src_run)
    src_run.mkdir(parents=True)

    print("прогоны JPlag:", flush=True)
    agg: dict[str, dict] = {}
    for lab in sorted(submissions):
        runs = submissions[lab]
        if not by_task:
            merged: dict[str, list[tuple[Path, Path]]] = {}
            for by_student in runs.values():
                for student, files in by_student.items():
                    merged.setdefault(student, []).extend(files)
            runs = {"all": merged}
        lab_key = f"lab-{lab:02d}"
        a = agg.setdefault(lab_key, {"students": set(), "pairs": [], "shown": []})
        for group in sorted(runs, key=group_sort):
            subs = runs[group]
            if len(subs) < 2:
                continue
            a["students"].update(subs)
            sub_root = src_run / f"{lab_key}--{group}"
            for student, files in subs.items():
                for path, rel in files:
                    dst = sub_root / student / rel
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    data = path.read_bytes()
                    if data != (stripped := data.rstrip()) + b"\n":
                        data = stripped + b"\n"
                    dst.write_bytes(data)
            result = OUT_DIR / lab_key / f"{group}.jplag"
            result.parent.mkdir(parents=True, exist_ok=True)
            try:
                proc = subprocess.run(
                    [java, "-jar", str(jar), "-M", "RUN", "-l", "python3", "-r", str(result), "--overwrite", str(sub_root)],
                    capture_output=True,
                    text=True,
                    timeout=900,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                failed += 1
                print(f"x {lab_key}/{group}: jplag завис (900 c)", file=sys.stderr)
                continue
            label = f"{lab_key}/{group}"
            if proc.returncode != 0:
                failed += 1
                print(f"x {label}: jplag rc={proc.returncode}\n{(proc.stdout + proc.stderr)[-800:]}", file=sys.stderr)
                continue
            unparsable = proc.stdout.count("ANTLR error - in")
            if unparsable:
                print(f"! {label}: ANTLR не смог разобрать {unparsable} файл(ов) — они не участвуют в сравнении", file=sys.stderr)
            shown, all_pairs = report_pairs(result, label, len(subs), min_sim)
            a["shown"].extend(shown)
            a["pairs"].extend({**p, "lab": lab_key} for p in all_pairs)

    if write_json:
        payload = {
            "generated_at": datetime.now(UTC).isoformat(timespec="minutes"),
            "checked": datetime.now(MSK).strftime("%d.%m.%Y %H:%M"),
            "min_sim": min_sim,
            "jplag": JPLAG_VERSION,
            "failed": failed,
            "labs": [
                {
                    "lab": lab,
                    "n": len(a["students"]),
                    "pairs": sorted(a["pairs"], key=lambda p: -p["avg"])[:MAX_PAIRS_IN_SUMMARY],
                    "shown": sorted(a["shown"], key=lambda p: -p["avg"])[:MAX_SHOWN_IN_SUMMARY],
                }
                for lab, a in sorted(agg.items())
            ],
        }
        JPLAG_SUMMARY_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\nsummary: {JPLAG_SUMMARY_PATH}", flush=True)
    return failed
