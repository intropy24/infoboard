
import json
from datetime import datetime
from itertools import pairwise
from pathlib import Path

from .jplag_chart import jplag_data


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


SPEED_JS = Path(__file__).with_name("widget.js").read_text(encoding="utf-8")


def speed_chart(charts: list[dict]) -> None:
    data = [c for c in charts if c["p"] or c.get("st")]
    if not data:
        return
    jb = jplag_data()
    jb_btns = '<button data-mode="jb-hist">⚖️ гистограмма</button><button data-mode="jb-graph">⚖️ граф</button>' if jb else ""
    print('<div id="speed-wrap">')
    print('<div id="speed-side">')
    print('<div id="speed-controls"><select id="sp-lab"></select>')
    print('<div id="sp-mode"><button data-mode="rate">стр/ч</button><button data-mode="lines" class="on">строк в коммите</button><button data-mode="heat">день×час</button><button data-mode="matrix">прогресс</button>' + jb_btns + '</div>')
    print('<div id="sp-times">')
    print('<label class="sp-dt"><input id="sp-from" type="text" placeholder="с: дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"><input type="datetime-local" tabindex="-1" aria-hidden="true"><button type="button" class="sp-cal" data-for="sp-from" title="выбрать дату">📅</button></label>')
    print('<label class="sp-dt"><input id="sp-to" type="text" placeholder="по: дд.мм.гггг чч:мм" spellcheck="false" autocomplete="off"><input type="datetime-local" tabindex="-1" aria-hidden="true"><button type="button" class="sp-cal" data-for="sp-to" title="выбрать дату">📅</button></label>')
    print('<div class="sp-ctl-row"><button id="sp-reset" title="показать всё время">×</button><button id="sp-scale" title="вертикальная шкала кривых">лог</button></div>')
    print('</div>')
    print('</div>')
    print('<ul id="sp-list">')
    for s in sorted({d["s"] for d in data}):
        print(f'<li data-student="{s}"><i></i>{s}</li>')
    print('</ul>')
    print('</div>')
    jb_layers = (
        '<canvas id="speed-jb"></canvas><canvas id="speed-jbg"></canvas><div id="sp-jb-info"></div>'
        if jb
        else ""
    )
    print('<div id="speed-box"><canvas id="speed-canvas"></canvas><canvas id="speed-heat"></canvas><canvas id="speed-mx"></canvas><div id="sp-heat-info">ячейка — коммиты за час (МСК); наведение на студента — его heatmap</div><div id="sp-mx-info">строка — студент, столбец — лаба; число — коммиты; клик по ячейке — последний коммит</div>' + jb_layers + '</div>')
    print('</div>')
    print()
    print('<script type="application/json" id="speed-data">' + json.dumps(data, ensure_ascii=False).replace("</", "<\\/") + '</script>')
    if jb:
        print()
        print('<script type="application/json" id="jplag-data">' + json.dumps(jb, ensure_ascii=False).replace("</", "<\\/") + '</script>')
    print()
    print('<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>')
    print()
    print('<script>' + SPEED_JS + '</script>')
    print()
