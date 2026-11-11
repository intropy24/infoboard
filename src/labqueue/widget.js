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
  var ylog = false; // вертикальная шкала кривых: по умолчанию линейная, кнопка #sp-scale переключает в лог
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
        y: { type: 'linear', title: { display: true, text: 'строк в коммите (лин)' },
             ticks: { callback: function (v) { // в лог-шкале подписываем только 1,2,5 · 10^n; в лин — все деления
                 if (ylog === false) return String(Math.round(v));
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
    if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); else if (mode === 'jb-hist') drawJbHist(); else if (mode === 'jb-graph') drawJbGraph(); // фильтры действуют и на оверлеи
  }
  (function () {
    var o = document.createElement('option'); o.value = ''; o.textContent = 'все лабы'; selL.appendChild(o);
    labs.forEach(function (v) { var e = document.createElement('option'); e.value = v; e.textContent = v; selL.appendChild(e); });
    selL.onchange = apply;
  })();
  var btns = document.querySelectorAll('#sp-mode button');
  // ⚖️ JPlag: данные рядом с speed-data (если summary закоммичен) — два дополнительных
  // режима виджета; нет данных — снимаем и кнопки, чтобы виджет не врал
  var JB = null;
  try {
    var _jbEl = document.getElementById('jplag-data');
    if (_jbEl) JB = JSON.parse(_jbEl.textContent);
  } catch (e) { JB = null; }
  if (!JB) Array.prototype.forEach.call(document.querySelectorAll('#sp-mode button[data-mode^="jb-"]'), function (b) { b.parentNode.removeChild(b); });
  btns = document.querySelectorAll('#sp-mode button');
  var jbCv = document.getElementById('speed-jb'), jbGc = document.getElementById('speed-jbg'), jbInfo = document.getElementById('sp-jb-info');
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
  var spScale = document.getElementById('sp-scale');
  function yTitle(m) { return (m === 'rate' ? 'скорость коммита, стр/ч' : 'строк в коммите') + (ylog ? ' (лог)' : ' (лин)'); }
  if (spScale) spScale.onclick = function () { // лог/лин: Chart.js сам перестроит деления, анимация приятная
    ylog = !ylog;
    chart.options.scales.y.type = ylog ? 'logarithmic' : 'linear';
    chart.options.scales.y.title.text = yTitle(mode);
    spScale.textContent = ylog ? 'лог' : 'лин';
    chart.update();
  };
  function setMode(m) { // переключение метрики Y / оверлеев: все величины уже лежат в точках
    mode = m;
    var heatOn = m === 'heat', mxOn = m === 'matrix', jbOn = m === 'jb-hist' || m === 'jb-graph';
    chart.canvas.style.display = heatOn || mxOn || jbOn ? 'none' : '';
    heat.style.display = heatOn ? 'block' : 'none';
    heatInfo.style.display = heatOn ? 'block' : 'none';
    mxCv.style.display = mxOn ? 'block' : 'none';
    mxInfo.style.display = mxOn ? 'block' : 'none';
    if (jbCv) jbCv.style.display = m === 'jb-hist' ? 'block' : 'none';
    if (jbGc) jbGc.style.display = m === 'jb-graph' ? 'block' : 'none';
    if (jbInfo) jbInfo.style.display = jbOn ? 'block' : 'none';
    Array.prototype.forEach.call(btns, function (b) { b.classList.toggle('on', b.getAttribute('data-mode') === m); });
    if (spScale) spScale.style.display = heatOn || mxOn || jbOn ? 'none' : '';
    if (heatOn) { drawHeat(); return; }
    if (mxOn) { drawMatrix(); return; }
    if (m === 'jb-hist') { drawJbHist(); return; }
    if (m === 'jb-graph') { drawJbGraph(); return; }
    ds.forEach(function (d) { d.data.forEach(function (p) { p.y = m === 'rate' ? (p.r === null ? 1 : p.r) : p.l; }); });
    chart.options.scales.y.title.text = yTitle(m);
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
    li.onmouseenter = function () { state.focus = s; glide(); if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); else if (mode === 'jb-graph') jbListHover(s); };
    li.onmouseleave = function () { state.focus = null; glide(); if (mode === 'heat') drawHeat(); else if (mode === 'matrix') drawMatrix(); else if (mode === 'jb-graph') jbListHover(null); };
    li.onclick = function () { // как чекбокс: один клик — видеть только его, ещё клик — добавить ещё одного
      if (picked[s]) { delete picked[s]; li.classList.remove('on'); }
      else { picked[s] = true; li.classList.add('on'); }
      apply();
    };
  });
  // ⚖️ JPlag: два режима виджета по данным jplag-data (summary, который выкладывает
  // jplag.yml). Лава выбирается общим селектором #sp-lab. Гистограмма — Chart.js
  // (полосы это объекты: свой hover/tooltip), граф — свой canvas: узлы по окружности,
  // физики и drag нет, хит-тесты арифметикой как у heatmap/матрицы
  var jbChart = null; // ленивая: Chart.js измеряет контейнер при создании, а hidden-контейнер нулевой
  var jbFocus = null; // студент, закреплённый кликом по узлу — остальные рёбра и узлы тускнеют
  var jbHov = null; // {node:i} | {edge:i} — что сейчас под курсором графа
  var jbGeom = null; // последний кадр {W,H,nodes,edges,ps} — для хит-тестов
  var JB_TOP_EDGES = 18; // рёбер в графе: топ по AVG; все пары — это одуванчик вместо смысла
  function jbPlural(n) { // пар/пара/пары
    if (n % 10 === 1 && n % 100 !== 11) return 'пара';
    if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return 'пары';
    return 'пар';
  }
  function jbPairs() { // пары выбранной лабы (пусто = все), сортированные по AVG; если слева
    // закреплены студенты — только пары с их участием, контрагенты остаются: именно
    // они и показывают, с кем закреплённый студент похож
    var ps = [], pmap = {}, pick = Object.keys(picked);
    pick.forEach(function (n) { pmap[n] = 1; });
    if (!JB) return ps;
    JB.labs.forEach(function (L) {
      if (!selL.value || L.lab === selL.value) L.pairs.forEach(function (p) {
        if (!pick.length || pmap[p.a] || pmap[p.b]) ps.push({ a: p.a, b: p.b, avg: p.avg, max: p.max, lab: L.lab });
      });
    });
    ps.sort(function (x, y) { return y.avg - x.avg; });
    return ps;
  }
  function drawJbHist() { // распределение AVG пар по бакетам ×10%, старший сверху — как Distribution в viewer JPlag
    if (!JB) return;
    var ps = jbPairs(), hist = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    ps.forEach(function (p) { hist[Math.min(9, Math.floor(p.avg / 10))]++; });
    hist.reverse(); // категория 0 у горизонтального Chart.js рисуется сверху
    var cs = getComputedStyle(document.documentElement);
    var cMut = cs.getPropertyValue('--muted').trim() || '#57606a';
    if (!jbChart) {
      jbChart = new Chart(jbCv, {
        type: 'bar',
        data: { labels: [], datasets: [{ data: [], backgroundColor: '#f85149', hoverBackgroundColor: '#ff7b72', borderWidth: 0, barPercentage: 0.8 }] },
        options: {
          indexAxis: 'y', responsive: true, maintainAspectRatio: false, animation: { duration: 120 },
          onClick: function () {}, // клик по столбцу ничего не фильтрует: таблица пар — в markdown над виджетом
          plugins: { legend: { display: false }, tooltip: { callbacks: { label: function (ctx) { var v = ctx.parsed.x; return v + ' ' + jbPlural(v); } } } },
          scales: {
            x: { ticks: { precision: 0, color: cMut }, grid: { color: cs.getPropertyValue('--border').trim() || '#d0d7de' }, title: { display: true, text: 'пар в бакете', color: cMut } },
            y: { ticks: { color: cMut }, grid: { display: false } }
          }
        }
      });
    }
    jbChart.data.labels = ['90–100%', '80–90%', '70–80%', '60–70%', '50–60%', '40–50%', '30–40%', '20–30%', '10–20%', '0–10%'];
    jbChart.data.datasets[0].data = hist;
    jbChart.update('none');
    var labNow = selL.value || 'все лабы';
    var has = !selL.value || JB.labs.some(function (L) { return L.lab === selL.value; });
    jbInfo.textContent = !has ? labNow + ' — нет JPlag-данных'
      : labNow + ' · пар ' + ps.length + (JB.checked ? ' · проверка от ' + JB.checked + ' МСК' : '') + ' · полоса — число пар в бакете среднего сходства (AVG)' + jbPickedHint();
  }
  var JBGHINT = 'узел — студент, ребро — похожесть пары (топ-' + JB_TOP_EDGES + ' по AVG); наведение — детали, клик по узлу — закрепить';
  function jbPickedHint() { // суффикс инфо-строк JPlag-режимов: кадр отфильтрован по закреплённым слева
    var pk = Object.keys(picked);
    return pk.length ? ' · закреплены: ' + pk.sort().join(', ') : '';
  }
  function jbHint() { return JBGHINT + jbPickedHint(); }
  function jbLayout() { // узлы по окружности (порядок по алфавиту — кадр детерминированный), рёбра — топ по AVG
    var ps = jbPairs(), seen = {}, order = [];
    ps.forEach(function (p) {
      if (!(p.a in seen)) { seen[p.a] = 1; order.push(p.a); }
      if (!(p.b in seen)) { seen[p.b] = 1; order.push(p.b); }
    });
    order.sort();
    var box = jbGc.parentElement, W = box.clientWidth, H = box.clientHeight;
    if (!W || !H) { jbGeom = null; return; }
    var R = Math.max(60, Math.min(W / 2 - 140, H / 2 - 52, 250)), cx = W / 2, cy = H / 2 + 6;
    var nodes = order.map(function (s, i) {
      var ang = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, order.length);
      return { s: s, x: cx + R * Math.cos(ang), y: cy + R * Math.sin(ang), ang: ang };
    });
    var idx = {};
    order.forEach(function (s, i) { idx[s] = i; });
    var edges = ps.slice(0, JB_TOP_EDGES).map(function (p) { return { a: idx[p.a], b: idx[p.b], avg: p.avg, max: p.max, lab: p.lab }; });
    jbGeom = { W: W, H: H, nodes: nodes, edges: edges, ps: ps };
  }
  function jbPaint() {
    if (!jbGeom) return;
    var W = jbGeom.W, H = jbGeom.H;
    var dpr = window.devicePixelRatio || 1;
    if (jbGc.width !== W * dpr || jbGc.height !== H * dpr) { jbGc.width = W * dpr; jbGc.height = H * dpr; }
    var c2 = jbGc.getContext('2d');
    c2.setTransform(dpr, 0, 0, dpr, 0, 0);
    c2.clearRect(0, 0, W, H);
    var cs = getComputedStyle(document.documentElement);
    var cMut = cs.getPropertyValue('--muted').trim() || '#57606a';
    var cFg = cs.getPropertyValue('--fg').trim() || '#1f2328';
    var nodes = jbGeom.nodes, edges = jbGeom.edges, top = edges.length ? edges[0].avg || 1 : 1;
    // hover и клик рисуют одинаковый «фокус»: наведение (с канваса или из списка) гасит
    // чужие рёбра и узлы так же, как закреплённый студент; наведение приоритетнее закрепления
    var foc = jbHov && jbHov.node != null ? nodes[jbHov.node].s : jbFocus;
    var nbr = null; // при фокусе ярки только сам студент и соседи по рёбрам
    if (foc) {
      nbr = {};
      nbr[foc] = 1;
      edges.forEach(function (ed) {
        if (nodes[ed.a].s === foc) nbr[nodes[ed.b].s] = 1;
        if (nodes[ed.b].s === foc) nbr[nodes[ed.a].s] = 1;
      });
    }
    var i;
    for (i = 0; i < edges.length; i++) {
      var e = edges[i];
      var rel = !foc || nodes[e.a].s === foc || nodes[e.b].s === foc;
      var hovE = jbHov && jbHov.edge === i;
      c2.globalAlpha = rel ? Math.min(1, 0.25 + 0.7 * (e.avg / top)) : 0.05;
      c2.strokeStyle = hovE ? '#4493f8' : '#f85149';
      c2.lineWidth = (hovE ? 1.5 : 0) + 0.8 + 2.6 * (e.avg / top);
      c2.beginPath();
      c2.moveTo(nodes[e.a].x, nodes[e.a].y);
      c2.lineTo(nodes[e.b].x, nodes[e.b].y);
      c2.stroke();
    }
    c2.font = '11px system-ui, sans-serif';
    c2.textBaseline = 'middle';
    for (i = 0; i < nodes.length; i++) {
      var n = nodes[i], hi = (jbHov && jbHov.node === i) || n.s === jbFocus;
      c2.globalAlpha = nbr && !nbr[n.s] ? 0.25 : 1;
      c2.beginPath();
      c2.arc(n.x, n.y, 6, 0, 2 * Math.PI);
      c2.fillStyle = 'hsl(' + hueOf(n.s) + ',72%,55%)';
      c2.fill();
      if (hi) { c2.strokeStyle = '#4493f8'; c2.lineWidth = 2; c2.stroke(); }
      var nm = n.s;
      while (nm.length > 2 && c2.measureText(nm + '…').width > 110) nm = nm.slice(0, -1);
      c2.fillStyle = hi ? cFg : cMut;
      var cos = Math.cos(n.ang);
      c2.textAlign = cos > 0.3 ? 'left' : cos < -0.3 ? 'right' : 'center';
      c2.fillText(nm, n.x + 11 * cos, n.y + 13 * Math.sin(n.ang));
    }
    c2.globalAlpha = 1;
  }
  function drawJbGraph() { jbLayout(); jbPaint(); jbInfo.textContent = jbHint(); }
  function jbListHover(s) { // наведение на имя в левом списке — та же подсветка, что от курсора на канвасе: колцо на узле + инфо-строка
    if (!jbGc || !jbGeom) return;
    if (s != null) {
      for (var i = 0; i < jbGeom.nodes.length; i++) {
        if (jbGeom.nodes[i].s === s) { jbHov = { node: i }; jbPaint(); jbInfo.textContent = jbInfoText(jbHov); return; }
      }
    }
    jbHov = null;
    jbPaint();
    jbInfo.textContent = jbHint();
  }
  function jbDist(px, py, x1, y1, x2, y2) { // расстояние от точки до отрезка — хит-тест рёбер
    var dx = x2 - x1, dy = y2 - y1, L = dx * dx + dy * dy;
    var t = L ? ((px - x1) * dx + (py - y1) * dy) / L : 0;
    t = Math.max(0, Math.min(1, t));
    var qx = x1 + t * dx - px, qy = y1 + t * dy - py;
    return Math.sqrt(qx * qx + qy * qy);
  }
  function jbHit(x, y) { // сначала узлы (попадание — круг), потом рёбра (±5px)
    if (!jbGeom) return null;
    var n = jbGeom.nodes, i;
    for (i = 0; i < n.length; i++) {
      var dx = x - n[i].x, dy = y - n[i].y;
      if (dx * dx + dy * dy <= 100) return { node: i };
    }
    for (i = 0; i < jbGeom.edges.length; i++) {
      var e = jbGeom.edges[i];
      if (jbDist(x, y, n[e.a].x, n[e.a].y, n[e.b].x, n[e.b].y) <= 5) return { edge: i };
    }
    return null;
  }
  function jbInfoText(hit) {
    if (!hit || !jbGeom) return jbHint();
    var n = jbGeom.nodes;
    if (hit.node != null) { // ps отсортирован по AVG — первая пара с этим студентом и есть максимальная
      var s = n[hit.node].s, cnt = 0, best = null;
      jbGeom.ps.forEach(function (p) { if (p.a === s || p.b === s) { cnt++; if (!best) best = p; } });
      return s + ' — пар ' + cnt + (best ? ', максимум ' + best.avg + '% (с ' + (best.a === s ? best.b : best.a) + ', ' + best.lab + ')' : '');
    }
    var e = jbGeom.edges[hit.edge];
    return n[e.a].s + ' ↔ ' + n[e.b].s + ' · AVG ' + e.avg + '% · MAX ' + e.max + '% · ' + e.lab;
  }
  if (jbGc) { // канвасов может не быть (summary не закоммичен) — слушатели вешаем только когда есть
    jbGc.addEventListener('mousemove', function (ev) {
      var rct = jbGc.getBoundingClientRect();
      var hit = jbHit(ev.clientX - rct.left, ev.clientY - rct.top);
      var same = !!(hit && jbHov && (hit.node != null ? jbHov.node === hit.node : jbHov.edge === hit.edge));
      if (!same) { jbHov = hit; jbPaint(); }
      jbGc.style.cursor = hit ? 'pointer' : 'default';
      jbInfo.textContent = jbInfoText(hit);
    });
    jbGc.addEventListener('mouseleave', function () { jbHov = null; jbPaint(); jbInfo.textContent = jbHint(); });
    jbGc.addEventListener('click', function () { // узел — закрепить/отпустить, фон — снять закрепление
      if (jbHov && jbHov.node != null) {
        var s = jbGeom.nodes[jbHov.node].s;
        jbFocus = jbFocus === s ? null : s;
      } else jbFocus = null;
      jbPaint();
    });
  }
})();
