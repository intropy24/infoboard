
import html
import json
import sys
from datetime import datetime

from .config import JPLAG_SUMMARY_PATH, MSK

MAX_ROWS = 15


def jplag_data() -> dict | None:
    try:
        raw = JPLAG_SUMMARY_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as e:
        print(f"jplag: не прочитать {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None
    try:
        doc = json.loads(raw)
    except ValueError as e:
        print(f"jplag: битый JSON в {JPLAG_SUMMARY_PATH}: {e}", file=sys.stderr)
        return None
    return doc if doc.get("labs") else None


def _subtitle(doc: dict) -> str:
    stamp = doc.get("checked") or ""
    if not stamp:
        try:
            stamp = datetime.fromisoformat(doc.get("generated_at", "")).astimezone(MSK).strftime("%d.%m.%Y %H:%M")
        except ValueError:
            stamp = "?"
    return f"_{doc.get('jplag', 'JPlag')}, проверка от {stamp} МСК, порог показа {doc['min_sim']:g}%_"


def _pairs_table(entry: dict, min_sim: float) -> None:
    lab = entry["lab"]
    shown = entry.get("shown") or []
    print(f"<h3>{html.escape(lab)}</h3>")
    print()
    if not shown:
        print(f'<p class="jb-empty">пар ≥ {min_sim:g}% нет</p>')
        print()
        return
    print(f'<table class="jb-t"><thead><tr><th>студент A</th><th>студент B</th><th>AVG</th><th>MAX</th><th>файлы</th></tr></thead><tbody>')
    for p in shown[:MAX_ROWS]:
        files = html.escape(", ".join(p["files"][:2])) + ("…" if len(p["files"]) > 2 else "")
        print(
            f"<tr><td>{html.escape(p['a'])}</td><td>{html.escape(p['b'])}</td>"
            f"<td>{p['avg']:g}%</td><td>{p['max']:g}%</td><td>{files or '—'}</td></tr>"
        )
    if len(shown) > MAX_ROWS:
        print(f'<tr class="jb-more"><td colspan="5">…и ещё {len(shown) - MAX_ROWS}</td></tr>')
    print("</tbody></table>")
    print()


def jplag_section() -> None:
    doc = jplag_data()
    if not doc:
        return
    print("## ⚖️ Сходство решений (JPlag)")
    print()
    print(_subtitle(doc))
    print("_гистограмма и граф пар — в интерактивном блоке ниже (кнопки «⚖️ гистограмма» / «⚖️ граф»)_")
    print()
    for entry in doc["labs"]:
        _pairs_table(entry, doc["min_sim"])
