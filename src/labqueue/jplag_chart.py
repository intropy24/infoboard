
import html
import json
import sys
from datetime import datetime
from pathlib import Path

from .config import JPLAG_SUMMARY_PATH, MSK

MAX_ROWS = 15

_X0, _X1 = 82, 560
_ROW = 24


def _bucket_range(b: int) -> str:
    return f"{b * 10}–{b * 10 + 10}%"


def _plural(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return "пара"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "пары"
    return "пар"


def _load() -> dict | None:
    try:
        raw = JPLAG_SUMMARY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"jplag: не прочитать {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None
    try:
        return json.loads(raw)
    except ValueError as e:
        print(f"jplag: битый JSON в {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None


def _subtitle(doc: dict) -> str:
    try:
        stamp = datetime.fromisoformat(doc.get("generated_at", "")).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
    except ValueError:
        stamp = "?"
    return f"_{doc.get('jplag', 'JPlag')}, проверка от {stamp} МСК, порог показа {doc['min_sim']:g}%_"


def _hist_svg(lab: str, hist: list[int]) -> list[str]:
    mx = max(hist) or 1
    rows = [f'<svg class="jb-svg" viewBox="0 0 640 {_ROW * 10 + 4}" role="img" aria-label="расхождение, lab {lab}">']
    for b in range(9, -1, -1):
        n, y = hist[b], 2 + (9 - b) * _ROW
        w = round(_X1 - _X0) * n // mx if n else 0
        rows.append(
            f'<g class="jb-bar" data-lab="{lab}" data-b="{b}" data-n="{n}">'
            f"<title>{_bucket_range(b)} — {n} {_plural(n)}</title>"
            f'<rect class="jb-hit" x="{_X0}" y="{y}" width="{_X1 - _X0}" height="{_ROW - 6}" fill="transparent" data-n="{n}"/>'
            f'<rect class="jb-val" x="{_X0}" y="{y}" width="{w}" height="{_ROW - 6}"/>'
            f'<text class="jb-label" x="{_X0 - 8}" y="{y + 13}" text-anchor="end">{_bucket_range(b)}</text>'
            f'<text class="jb-count" x="{_X1 + 8}" y="{y + 13}">{n or ""}</text>'
            "</g>"
        )
    rows.append("</svg>")
    return rows


def _pairs_table(lab: str, groups: list[dict], min_sim: float) -> list[str]:
    pairs = [p for g in groups for p in g["pairs"]]
    pairs.sort(key=lambda p: -p["avg"])
    if not pairs:
        return [f'<p class="jb-empty">пар ≥ {min_sim:g}% нет</p>']
    rows = [f'<table class="jb-t" data-lab="{lab}"><thead><tr><th>студент A</th><th>студент B</th><th>AVG</th><th>MAX</th><th>файлы</th></tr></thead><tbody>']
    for p in pairs[:MAX_ROWS]:
        b = min(9, int(p["avg"] // 10))
        files = html.escape(", ".join(p["files"][:2])) + ("…" if len(p["files"]) > 2 else "")
        rows.append(
            f'<tr data-lab="{lab}" data-b="{b}">'
            f"<td>{html.escape(p['a'])}</td><td>{html.escape(p['b'])}</td>"
            f"<td>{p['avg']:g}%</td><td>{p['max']:g}%</td><td>{files or '—'}</td></tr>"
        )
    if len(pairs) > MAX_ROWS:
        rows.append(f'<tr class="jb-more"><td colspan="5">…и ещё {len(pairs) - MAX_ROWS}</td></tr>')
    rows.append("</tbody></table>")
    return rows


def jplag_section() -> None:
    doc = _load()
    if not doc or not doc.get("labs"):
        return
    print("## ⚖️ Сходство решений (JPlag)")
    print()
    print(_subtitle(doc))
    print()
    for entry in doc["labs"]:
        lab = entry["lab"]
        print(f'<div class="jb-lab" id="jb-{lab}">')
        print(f"<h3>{html.escape(lab)}</h3>")
        hist = [0] * 10
        for g in entry["groups"]:
            for b, n in enumerate(g["hist"]):
                hist[b] += n
        print(*_hist_svg(lab, hist), sep="\n")
        print('<div class="jb-info" hidden></div>')
        print(*_pairs_table(lab, entry["groups"], doc["min_sim"]), sep="\n")
        print("</div>")
        print()
    try:
        js = Path(__file__).with_name("jplag.js").read_text(encoding="utf-8")
    except OSError as e:
        print(f"jplag: нет jplag.js ({e}) — секция без интерактива", file=sys.stderr)
        return
    print("<script>" + js + "</script>")
