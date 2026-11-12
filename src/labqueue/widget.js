/*
 * Виджет бора «⚡ скорость работы» и «⚖️ сходство решений».
 *
 * Работает без сборки (ES5) поверх данных из соседних JSON-скриптов на странице:
 * #speed-data — коммиты по веткам, #jplag-data — опциональная сводка JPlag.
 * Режимы: кривые темпа, heatmap «день × час», матрица прогресса, гистограмма
 * и граф сходства. Регрессии ловит смоук: tools/widget_smoke.js.
 */
(function () {
  // ═══════════════ 1. Данные, палитра, форматтеры ═══════════════

  var dataEl = document.getElementById('speed-data');
  if (!dataEl || !window.Chart) return;
  var commits = JSON.parse(dataEl.textContent);

  // Оттенки студентов: имена расставляются по хэшу и получают равные шаги по кругу,
  // чтобы цвета не слипались у соседних в списке студентов.
  var hues = {};
  (function assignHues() {
    function hash(name) {
      var v = 0;
      for (var i = 0; i < name.length; i++) v = (v * 31 + name.charCodeAt(i)) % 2147483647;
      return v;
    }
    var seen = {};
    var names = commits
      .map(function (c) {
        return c.s;
      })
      .filter(function (name) {
        if (seen[name]) return false;
        seen[name] = 1;
        return true;
      });
    names.sort(function (a, b) {
      return hash(a) - hash(b);
    });
    names.forEach(function (name, i) {
      hues[name] = Math.round((i * 360) / names.length) % 360;
    });
  })();

  function hueOf(name) {
    return name in hues ? hues[name] : 0;
  }

  var fmtTick = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: 'short' });
  var fmtTip = new Intl.DateTimeFormat('ru-RU', { day: 'numeric', month: '2-digit', hour: '2-digit', minute: '2-digit' });

  function pad2(n) {
    return (n < 10 ? '0' : '') + n;
  }

  function cssVar(name, fallback) {
    var value = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    return value || fallback;
  }

  function bodyIsDark() {
    var rgb = (getComputedStyle(document.body).backgroundColor.match(/\d+/g) || [255, 255, 255]).map(Number);
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2] < 140;
  }

  // Текст поверх цветной ячейки: светлый на тёмных уровнях палитры, тёмный на ярких.
  function readableText(hex, dark) {
    var luma = 0.299 * parseInt(hex.slice(1, 3), 16) + 0.587 * parseInt(hex.slice(3, 5), 16) + 0.114 * parseInt(hex.slice(5, 7), 16);
    return luma > 140 ? (dark ? '#0d1117' : '#1f2328') : '#fff';
  }

  // ═══════════════ 2. Состояние ═══════════════

  var hover = { student: null }; // имя под курсором левого списка
  var mode = 'lines'; // 'rate' | 'lines' | 'heat' | 'matrix' | 'jb-hist' | 'jb-graph'
  var ylog = false; // вертикальная шкала кривых (кнопка #sp-scale)
  var picked = {}; // закреплённые кликом списка студенты: пусто = все
  var timeRange = { from: null, to: null }; // окно «с/по» в мс
  var drag = null; // выделение рамкой: {x0, x1} в пикселях канваса
  var draggedOnce = false; // погасить клик после перетаскивания рамки
  var pointer = null; // {x, y} над полем графика — для перекрестия

  // ═══════════════ 3. Основной график (кривые) ═══════════════

  function targetAlpha(series) {
    if (series.hidden) return 0.04;
    if (hover.student) return series.student === hover.student ? 0.95 : 0.04;
    return 0.85;
  }

  // scriptable-цвет: читает текущую альф _alpha, которую плавно ведёт rAF-анимация.
  function seriesColor(ctx) {
    var series = ctx.dataset;
    var lightness = hover.student && series.student === hover.student ? 62 : 55;
    return 'hsla(' + series.hue + ',72%,' + lightness + '%,' + series._alpha + ')';
  }

  // Точка = коммит: x — время, y — метрика текущего режима, r — темп стр/ч
  // (null, если не измерим), l — строки, u — ссылка, h/m — автор и сообщение.
  var series = commits.map(function (c) {
    return {
      label: c.s + ' · ' + c.b,
      student: c.s,
      branch: c.b,
      hue: hueOf(c.s),
      _alpha: 0.85,
      data: c.p.map(function (pt) {
        return { x: pt[0], y: pt[5], r: pt[1], l: pt[5], u: pt[2], h: pt[3], m: pt[4] };
      }),
      borderColor: seriesColor,
      backgroundColor: seriesColor,
      pointBackgroundColor: seriesColor,
      pointBorderColor: 'transparent',
      pointBorderWidth: 0,
      pointRadius: 2.5,
      pointHoverRadius: 4.5,
      borderWidth: 1.4,
      tension: 0
    };
  });

  var crosshair = {
    id: 'sp-crosshair',
    afterDraw: function (ch) {
      if (!pointer) return;
      var g = ch.ctx,
        area = ch.chartArea;
      g.save();
      g.strokeStyle = 'rgba(139,148,158,.55)';
      g.setLineDash([4, 4]);
      g.beginPath();
      g.moveTo(pointer.x + 0.5, area.top);
      g.lineTo(pointer.x + 0.5, area.bottom);
      g.stroke();
      g.beginPath();
      g.moveTo(area.left, pointer.y + 0.5);
      g.lineTo(area.right, pointer.y + 0.5);
      g.stroke();
      g.restore();
    }
  };

  var dragRect = {
    id: 'sp-drag-rect',
    afterDraw: function (ch) {
      if (!drag) return;
      var g = ch.ctx,
        area = ch.chartArea;
      var left = Math.min(drag.x0, drag.x1),
        width = Math.abs(drag.x1 - drag.x0);
      g.save();
      g.fillStyle = 'rgba(56,139,253,.15)';
      g.fillRect(left, area.top, width, area.bottom - area.top);
      g.strokeStyle = 'rgba(56,139,253,.7)';
      g.strokeRect(left + 0.5, area.top + 0.5, width, area.bottom - area.top - 1);
      g.restore();
    }
  };

  // Деления Y: в лог-шкале подписываем только 1, 2, 5 · 10^n, в линейной — все.
  function yTickLabel(v) {
    if (ylog === false) return String(Math.round(v));
    var exp = Math.round(Math.log10(v));
    var mantissa = v / Math.pow(10, exp);
    if (Math.abs(mantissa - 1) < 1e-9 || Math.abs(mantissa - 2) < 1e-9 || Math.abs(mantissa - 5) < 1e-9) return String(Math.round(v));
    return null;
  }

  function yTitle(m) {
    return (m === 'rate' ? 'скорость коммита, стр/ч' : 'строк в коммите') + (ylog ? ' (лог)' : ' (лин)');
  }

  function tooltipLabel(ctx) {
    var point = ctx.raw;
    if (mode === 'rate' && point.r === null)
      return ctx.dataset.label + ': +' + point.l + ' строк — темп не измерим (первый или одновременный коммит)';
    if (mode === 'rate') return ctx.dataset.label + ': +' + point.l + ' строк → ' + ctx.parsed.y + ' стр/ч';
    return ctx.dataset.label + ': +' + point.l + ' строк';
  }

  var chart = new Chart(document.getElementById('speed-canvas'), {
    type: 'line',
    data: { datasets: series },
    plugins: [dragRect, crosshair],
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      // подсветка коммита — только когда вертикаль пересекла его, как перекрестие
      interaction: { mode: 'nearest', axis: 'x', intersect: false },
      onClick: function (e, elements, ch) {
        if (draggedOnce) {
          draggedOnce = false;
          return;
        }
        if (!elements.length) return;
        var point = ch.data.datasets[elements[0].datasetIndex].data[elements[0].index];
        if (point && point.u) window.open(point.u, '_blank');
      },
      onHover: function (e, elements) {
        if (e.native && e.native.target && !drag) e.native.target.style.cursor = elements.length ? 'pointer' : 'default';
      },
      scales: {
        x: {
          type: 'linear',
          ticks: {
            maxRotation: 0,
            autoSkip: true,
            callback: function (v) {
              return fmtTick.format(new Date(v));
            }
          },
          grid: { color: 'rgba(128,128,128,.15)' }
        },
        y: {
          type: 'linear',
          title: { display: true, text: 'строк в коммите (лин)' },
          ticks: { callback: yTickLabel },
          grid: { color: 'rgba(128,128,128,.15)' }
        }
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          callbacks: {
            title: function (items) {
              return items.length ? fmtTip.format(new Date(items[0].parsed.x)) : '';
            },
            label: tooltipLabel,
            afterLabel: function (ctx) {
              return ctx.raw.m ? '«' + ctx.raw.m + '»' : '';
            }
          }
        }
      }
    }
  });

  // Плавная подсветка: альфа каждой кривой за 100 мс ведётся к цели (easeOutQuad).
  // Шаги идут через chart.update('none'), не render(): Chart.js пересчитывает
  // scriptable-цвета только в update(), render() рисует старым кэшем.
  var glideReq = 0;
  function glide() {
    cancelAnimationFrame(glideReq);
    var t0 = performance.now();
    var from = series.map(function (s) {
      return s._alpha;
    });
    var to = series.map(targetAlpha);
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100);
      var eased = k * (2 - k);
      series.forEach(function (s, i) {
        s._alpha = from[i] + (to[i] - from[i]) * eased;
      });
      chart.update('none');
      if (k < 1) glideReq = requestAnimationFrame(step);
    })(t0);
  }

  // ═══════════════ 4. Фильтры: лаба и закреплённые студенты ═══════════════

  var labSelect = document.getElementById('sp-lab');
  var labNames = [];
  commits.forEach(function (c) {
    if (labNames.indexOf(c.b) < 0) labNames.push(c.b);
  });
  labNames.sort();

  (function fillLabOptions() {
    var all = document.createElement('option');
    all.value = '';
    all.textContent = 'все лабы';
    labSelect.appendChild(all);
    labNames.forEach(function (lab) {
      var option = document.createElement('option');
      option.value = lab;
      option.textContent = lab;
      labSelect.appendChild(option);
    });
    labSelect.onchange = applyFilters;
  })();

  function applyFilters() {
    var anyPicked = Object.keys(picked).length > 0;
    series.forEach(function (s) {
      var studentOk = !anyPicked || picked[s.student];
      var labOk = !labSelect.value || s.branch === labSelect.value;
      s.hidden = !(studentOk && labOk);
    });
    chart.update('none');
    glide();
    if (mode === 'heat') drawHeat();
    else if (mode === 'matrix') drawMatrix();
    else if (mode === 'jb-hist') drawJplagHist();
    else if (mode === 'jb-graph') drawJplagGraph();
  }

  // ═══════════════ 5. Окно времени «с/по» ═══════════════

  var inputFrom = document.getElementById('sp-from');
  var inputTo = document.getElementById('sp-to');

  function toInput(ms) {
    if (ms == null) return '';
    var d = new Date(ms);
    return pad2(d.getDate()) + '.' + pad2(d.getMonth() + 1) + '.' + d.getFullYear() + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  // Парсит «дд.мм.гггг[ чч:мм]» руками: null — пусто, NaN — мусор (подсветим красным).
  function parseInput(value) {
    value = (value || '').trim();
    if (!value) return null;
    var m = value.match(/^(\d{1,2})[.\/-](\d{1,2})[.\/-](\d{4})(?:[ ,]+(\d{1,2})(?::(\d{1,2}))?)?$/);
    if (!m) return NaN;
    var d = new Date(+m[3], +m[2] - 1, +m[1], +(m[4] || 0), +(m[5] || 0));
    if (d.getFullYear() !== +m[3] || d.getMonth() !== +m[2] - 1 || d.getDate() !== +m[1]) return NaN; // 31.06 Date перекатывает на июль — это мусор
    return d.getTime();
  }

  function applyTime(from, to) {
    timeRange.from = from == null ? null : from;
    timeRange.to = to == null ? null : to;
    chart.options.scales.x.min = from == null ? undefined : from;
    chart.options.scales.x.max = to == null ? undefined : to;
    inputFrom.value = toInput(from);
    inputTo.value = toInput(to);
    chart.update('none');
    if (mode === 'heat') drawHeat();
  }

  function onInputTime() {
    var from = parseInput(inputFrom.value),
      to = parseInput(inputTo.value);
    inputFrom.classList.toggle('bad', isNaN(from));
    inputTo.classList.toggle('bad', isNaN(to));
    if (isNaN(from) || isNaN(to)) return;
    if (from != null && to != null && from >= to) return; // перевёрнутый диапазон — ждём доводки
    applyTime(from, to);
  }

  inputFrom.onchange = onInputTime;
  inputTo.onchange = onInputTime;
  inputFrom.onkeydown = inputTo.onkeydown = function (e) {
    if (e.key === 'Enter') onInputTime();
  };

  document.getElementById('sp-reset').onclick = function () {
    applyTime(null, null);
  };

  // 📅 открывает нативный datetime-local, спрятанный под текстовым полем.
  function toLocalISO(ms) {
    var d = new Date(ms);
    return d.getFullYear() + '-' + pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + 'T' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
  }

  Array.prototype.forEach.call(document.querySelectorAll('.sp-cal'), function (btn) {
    var text = document.getElementById(btn.getAttribute('data-for'));
    var native = text.parentElement.querySelector('input[type="datetime-local"]');
    btn.onclick = function () {
      var ms = parseInput(text.value);
      native.value = ms === null || isNaN(ms) ? '' : toLocalISO(ms);
      try {
        native.showPicker();
      } catch (e) {
        native.focus();
      }
    };
    native.onchange = function () {
      text.value = native.value ? toInput(new Date(native.value).getTime()) : '';
      text.classList.remove('bad');
      onInputTime();
    };
  });

  // Перетаскивание рамкой по графику = зум по X; перекрестие следует за курсором.
  var canvas = chart.canvas;
  canvas.addEventListener('mousedown', function (e) {
    if (e.button !== 0) return;
    var rect = canvas.getBoundingClientRect();
    drag = { x0: e.clientX - rect.left, x1: e.clientX - rect.left };
    canvas.style.cursor = 'crosshair';
  });
  canvas.addEventListener('mousemove', function (e) {
    var rect = canvas.getBoundingClientRect(),
      area = chart.chartArea;
    var x = e.clientX - rect.left,
      y = e.clientY - rect.top;
    pointer = x >= area.left && x <= area.right && y >= area.top && y <= area.bottom ? { x: x, y: y } : null;
    if (drag) drag.x1 = Math.max(area.left, Math.min(area.right, x));
    chart.render();
  });
  canvas.addEventListener('mouseleave', function () {
    if (pointer) {
      pointer = null;
      chart.render();
    }
  });
  window.addEventListener('mouseup', function () {
    if (!drag) return;
    var left = Math.min(drag.x0, drag.x1),
      right = Math.max(drag.x0, drag.x1);
    drag = null;
    canvas.style.cursor = 'default';
    if (right - left < 5) {
      chart.render();
      return;
    } // обычный клик, не выделение
    draggedOnce = true;
    applyTime(chart.scales.x.getValueForPixel(left), chart.scales.x.getValueForPixel(right));
  });

  // Переключатель линейной/логарифмической шкалы Y.
  var scaleButton = document.getElementById('sp-scale');
  if (scaleButton)
    scaleButton.onclick = function () {
      ylog = !ylog;
      chart.options.scales.y.type = ylog ? 'logarithmic' : 'linear';
      chart.options.scales.y.title.text = yTitle(mode);
      scaleButton.textContent = ylog ? 'лог' : 'лин';
      chart.update();
    };

  // ═══════════════ 6. Список студентов (слева) ═══════════════

  Array.prototype.forEach.call(document.getElementById('sp-list').children, function (li) {
    var name = li.getAttribute('data-student');
    li.querySelector('i').style.background = 'hsl(' + hueOf(name) + ',72%,55%)';
    li.onmouseenter = function () {
      hover.student = name;
      glide();
      if (mode === 'heat') drawHeat();
      else if (mode === 'matrix') drawMatrix();
      else if (mode === 'jb-graph') jplagListHover(name);
    };
    li.onmouseleave = function () {
      hover.student = null;
      glide();
      if (mode === 'heat') drawHeat();
      else if (mode === 'matrix') drawMatrix();
      else if (mode === 'jb-graph') jplagListHover(null);
    };
    li.onclick = function () {
      // чекбокс: клик — видеть только его, ещё клик — добавить
      if (picked[name]) {
        delete picked[name];
        li.classList.remove('on');
      } else {
        picked[name] = true;
        li.classList.add('on');
      }
      applyFilters();
    };
  });

  // ═══════════════ 7. Переключение режимов ═══════════════

  var jplag = null;
  try {
    var jplagEl = document.getElementById('jplag-data');
    if (jplagEl) jplag = JSON.parse(jplagEl.textContent);
  } catch (e) {
    jplag = null;
  }

  var modeButtons = document.querySelectorAll('#sp-mode button');
  if (!jplag) {
    // нет сводки — снимаем и ⚖️-кнопки, чтобы виджет не врал
    Array.prototype.forEach.call(document.querySelectorAll('#sp-mode button[data-mode^="jb-"]'), function (btn) {
      btn.parentNode.removeChild(btn);
    });
    modeButtons = document.querySelectorAll('#sp-mode button');
  }

  var histCanvas = document.getElementById('speed-jb');
  var graphCanvas = document.getElementById('speed-jbg');
  var jplagInfo = document.getElementById('sp-jb-info');
  var thresholdInput = document.getElementById('jb-min-edge');
  var thresholdLabel = document.getElementById('jb-min-edge-lbl');
  var thresholdRow = document.getElementById('sp-jb-thr');

  function setMode(next) {
    mode = next;
    var heatOn = mode === 'heat',
      matrixOn = mode === 'matrix';
    var jplagOn = mode === 'jb-hist' || mode === 'jb-graph';
    chart.canvas.style.display = heatOn || matrixOn || jplagOn ? 'none' : '';
    heat.style.display = heatOn ? 'block' : 'none';
    heatInfo.style.display = heatOn ? 'block' : 'none';
    matrixCanvas.style.display = matrixOn ? 'block' : 'none';
    matrixInfo.style.display = matrixOn ? 'block' : 'none';
    if (histCanvas) histCanvas.style.display = mode === 'jb-hist' ? 'block' : 'none';
    if (graphCanvas) graphCanvas.style.display = mode === 'jb-graph' ? 'block' : 'none';
    if (jplagInfo) jplagInfo.style.display = jplagOn ? 'block' : 'none';
    if (thresholdRow) thresholdRow.style.display = mode === 'jb-graph' ? 'flex' : 'none';
    Array.prototype.forEach.call(modeButtons, function (btn) {
      btn.classList.toggle('on', btn.getAttribute('data-mode') === mode);
    });
    if (scaleButton) scaleButton.style.display = heatOn || matrixOn || jplagOn ? 'none' : '';

    if (heatOn) return drawHeat();
    if (matrixOn) return drawMatrix();
    if (mode === 'jb-hist') return drawJplagHist();
    if (mode === 'jb-graph') return drawJplagGraph();
    // кривые: метрика Y уже лежит в точках, остаётся переложить y
    series.forEach(function (s) {
      s.data.forEach(function (p) {
        p.y = mode === 'rate' ? (p.r === null ? 1 : p.r) : p.l;
      });
    });
    chart.options.scales.y.title.text = yTitle(mode);
    chart.update('none');
  }

  Array.prototype.forEach.call(modeButtons, function (btn) {
    btn.onclick = function () {
      setMode(btn.getAttribute('data-mode'));
    };
  });

  // ═══════════════ 8. Heatmap «день недели × час» ═══════════════

  var HEAT_HINT = 'ячейка — коммиты за час (МСК); наведение на студента — его heatmap';
  var heat = document.getElementById('speed-heat');
  var heatInfo = document.getElementById('sp-heat-info');
  var DAYS = ['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'];
  var MARGIN_LEFT = 34,
    MARGIN_TOP = 8,
    MARGIN_RIGHT = 8,
    MARGIN_BOTTOM = 36;
  var TOTAL_GAP = 12; // отступы перед итоговой колонкой и строкой

  var heatGeom = null; // {cw, ch} — превращать пиксели мыши в номер ячейки
  var heatHover = null; // {day, hour} под курсором (day: 0 = вс)
  var heatTarget = {}; // целевые клетки — подсказка показывает цель, не середину твина
  var heatShown = {}; // клетки, нарисованные сейчас — твин интерполирует от них
  var heatReq = 0;

  // {day*24+hour: {n, who}} по коммитам видимых серий в окне «с/по».
  function heatCells() {
    var cells = {};
    series.forEach(function (s) {
      if (s.hidden) return;
      if (hover.student && s.student !== hover.student) return; // изоляция по наведению — как у кривых
      s.data.forEach(function (p) {
        if (timeRange.from != null && p.x < timeRange.from) return;
        if (timeRange.to != null && p.x > timeRange.to) return;
        var t = new Date(p.x + 108e5); // МСК: +3 ч к unix-времени и читаем UTC-поля
        var key = t.getUTCDay() * 24 + t.getUTCHours();
        var cell = cells[key] || (cells[key] = { n: 0, who: {} });
        cell.n += 1;
        cell.who[s.student] = (cell.who[s.student] || 0) + 1;
      });
    });
    return cells;
  }

  // Та же плавность, что у кривых: ведём видимые клетки к цели за 100 мс.
  function drawHeat() {
    var target = (heatTarget = heatCells());
    cancelAnimationFrame(heatReq);
    var from = heatShown,
      t0 = performance.now(),
      keys = {};
    Object.keys(from).forEach(function (k) {
      keys[k] = 1;
    });
    Object.keys(target).forEach(function (k) {
      keys[k] = 1;
    });
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100),
        eased = k * (2 - k),
        mid = {};
      Object.keys(keys).forEach(function (key) {
        var a = (from[key] && from[key].n) || 0,
          b = (target[key] && target[key].n) || 0;
        var value = a + (b - a) * eased;
        if (value > 0.02) mid[key] = { n: value, who: (target[key] || from[key]).who };
      });
      heatShown = mid;
      heatPaint();
      if (k < 1) heatReq = requestAnimationFrame(step);
      else {
        heatShown = target;
        heatPaint();
      }
    })(t0);
  }

  // sqrt-шкала уровней: редкие часы не тонут на фоне пиков.
  function heatLevel(n, max) {
    if (!n) return 0;
    var q = Math.sqrt(n / max);
    return q > 0.75 ? 4 : q > 0.5 ? 3 : q > 0.25 ? 2 : 1;
  }

  function heatCell(g, x, y, cw, ch, value, level, palette, dark) {
    g.fillStyle = palette[level];
    g.fillRect(x + 1, y + 1, cw - 2, ch - 2);
    if (!value) return;
    g.fillStyle = readableText(palette[level], dark);
    g.textAlign = 'center';
    g.fillText(String(Math.round(value)), x + cw / 2, y + ch / 2);
  }

  function heatPaint() {
    var box = heat.parentElement,
      W = box.clientWidth,
      H = box.clientHeight;
    if (!W || !H) return;
    var dpr = window.devicePixelRatio || 1;
    if (heat.width !== W * dpr || heat.height !== H * dpr) {
      heat.width = W * dpr;
      heat.height = H * dpr;
    } // под HiDPI, иначе мыло
    var g = heat.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);

    var muted = cssVar('--muted', '#57606a');
    var dark = bodyIsDark();
    var palette = dark ? ['#161b22', '#0e4429', '#006d32', '#26a641', '#39d353'] : ['#ebedf0', '#9be9a8', '#40c463', '#30a14e', '#216e39']; // канон зелёных GitHub-календаря
    var cells = heatShown,
      max = 1;
    Object.keys(cells).forEach(function (k) {
      max = Math.max(max, cells[k].n);
    });

    // 24 колонки часов + 25-я «итог по дням»; 7 строк дней + 1 «итог по часам»
    var cw = (W - MARGIN_LEFT - MARGIN_RIGHT - TOTAL_GAP) / 25;
    var ch = (H - MARGIN_TOP - MARGIN_BOTTOM - TOTAL_GAP) / 8;
    var totalsX = MARGIN_LEFT + 24 * cw + TOTAL_GAP,
      totalsY = MARGIN_TOP + 7 * ch + TOTAL_GAP;
    heatGeom = { cw: cw, ch: ch };
    g.font = '11px system-ui, sans-serif';
    g.textBaseline = 'middle';

    for (var row = 0; row < 7; row++) {
      var day = (row + 1) % 7; // строки сверху вниз: пн..вс
      g.fillStyle = muted;
      g.textAlign = 'right';
      g.fillText(DAYS[row], MARGIN_LEFT - 6, MARGIN_TOP + ch * (row + 0.5));
      for (var hour = 0; hour < 24; hour++) {
        var cell = cells[day * 24 + hour];
        var value = cell ? cell.n : 0;
        heatCell(g, MARGIN_LEFT + hour * cw, MARGIN_TOP + row * ch, cw, ch, value, heatLevel(value, max), palette, dark);
      }
    }

    // Итоги считаются по своим шкалам: на фоне семикратных сумм дни/часы потемнели бы вровень.
    function totalsRow() {
      var totals = [],
        maxT = 1;
      for (var h = 0; h < 24; h++) totals[h] = 0;
      Object.keys(cells).forEach(function (k) {
        totals[+k % 24] += cells[k].n;
      });
      for (var h2 = 0; h2 < 24; h2++) maxT = Math.max(maxT, totals[h2]);
      return { totals: totals, max: maxT };
    }
    function totalsColumn() {
      var totals = [],
        maxT = 1,
        all = 0;
      Object.keys(cells).forEach(function (k) {
        var d = Math.floor(+k / 24);
        totals[d] = (totals[d] || 0) + cells[k].n;
        all += cells[k].n;
      });
      for (var d2 = 0; d2 < 7; d2++) {
        totals[d2] = totals[d2] || 0;
        maxT = Math.max(maxT, totals[d2]);
      }
      return { totals: totals, max: maxT, all: all };
    }

    g.strokeStyle = dark ? 'rgba(139,148,158,.35)' : 'rgba(87,96,106,.3)';
    g.beginPath();
    g.moveTo(MARGIN_LEFT, totalsY - TOTAL_GAP / 2 + 0.5);
    g.lineTo(totalsX - TOTAL_GAP / 2, totalsY - TOTAL_GAP / 2 + 0.5);
    g.moveTo(totalsX - TOTAL_GAP / 2 + 0.5, MARGIN_TOP);
    g.lineTo(totalsX - TOTAL_GAP / 2 + 0.5, totalsY + ch);
    g.stroke();
    g.fillStyle = muted;
    g.textAlign = 'right';
    g.fillText('итог', MARGIN_LEFT - 6, totalsY + ch / 2);

    var hours = totalsRow();
    for (var h = 0; h < 24; h++) {
      heatCell(g, MARGIN_LEFT + h * cw, totalsY, cw, ch, hours.totals[h], heatLevel(hours.totals[h], hours.max), palette, dark);
    }
    var days = totalsColumn();
    for (var d = 0; d < 7; d++) {
      // порядок строк тот же, что у дней: пн..вс
      var dayValue = days.totals[(d + 1) % 7];
      heatCell(g, totalsX, MARGIN_TOP + d * ch, cw, ch, dayValue, heatLevel(dayValue, days.max), palette, dark);
    }

    // Угол сетки — total всех коммитов muted-цифрой на пустой клетке; подписи часов — в нижней полосе.
    g.fillStyle = palette[0];
    g.fillRect(totalsX + 1, totalsY + 1, cw - 2, ch - 2);
    g.fillStyle = muted;
    g.textAlign = 'center';
    if (days.all) g.fillText(String(Math.round(days.all)), totalsX + cw / 2, totalsY + ch / 2);
    g.fillText('итог', totalsX + cw / 2, H - MARGIN_BOTTOM + 8);
    for (var h3 = 0; h3 < 24; h3 += 2) g.fillText(String(h3), MARGIN_LEFT + h3 * cw + cw / 2, H - MARGIN_BOTTOM + 8);

    if (heatHover) {
      g.strokeStyle = '#4493f8';
      g.lineWidth = 2;
      g.strokeRect(MARGIN_LEFT + heatHover.hour * cw + 1.5, MARGIN_TOP + ((heatHover.day + 6) % 7) * ch + 1.5, cw - 3, ch - 3);
    }
  }

  heat.addEventListener('mousemove', function (e) {
    if (!heatGeom) return;
    var rect = heat.getBoundingClientRect();
    var hour = Math.floor((e.clientX - rect.left - MARGIN_LEFT) / heatGeom.cw);
    var row = Math.floor((e.clientY - rect.top - MARGIN_TOP) / heatGeom.ch);
    var cell = hour >= 0 && hour < 24 && row >= 0 && row < 7 ? { hour: hour, day: (row + 1) % 7 } : null;
    var same = !!(cell && heatHover && cell.day === heatHover.day && cell.hour === heatHover.hour);
    if (!same && (cell || heatHover)) {
      heatHover = cell;
      heatPaint();
    } // перерисовка только при смене ячейки
    if (!cell) {
      heatInfo.textContent = HEAT_HINT;
      return;
    }
    var span = DAYS[(cell.day + 6) % 7] + ' ' + cell.hour + ':00–' + ((cell.hour + 1) % 24) + ':00';
    var data = heatTarget[cell.day * 24 + cell.hour];
    if (!data) {
      heatInfo.textContent = span + ' — пусто';
      return;
    }
    var who = Object.keys(data.who);
    heatInfo.textContent =
      span + ' — ' + data.n + ' комм.: ' + who.slice(0, 4).join(', ') + (who.length > 4 ? ' +' + (who.length - 4) : '');
  });
  heat.addEventListener('mouseleave', function () {
    heatHover = null;
    if (mode === 'heat') {
      heatPaint();
      heatInfo.textContent = HEAT_HINT;
    }
  });

  // Тема переключается на <html data-theme> — перекрашиваем оверлеи на лету.
  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (mode === 'heat') drawHeat();
      else if (mode === 'matrix') drawMatrix();
    }).observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] });
  }

  // ═══════════════ 9. Матрица прогресса ═══════════════

  var matrixCanvas = document.getElementById('speed-mx');
  var matrixInfo = document.getElementById('sp-mx-info');
  var MATRIX_HINT = 'строка — студент, столбец — лаба; число — коммиты; клик по ячейке — последний коммит';
  var STATUS_COLOR = { merged: '#3fb950', queue: '#d29922', reviewed: '#58a6ff', nopr: '#8b949e' };
  var STATUS_TEXT = { merged: 'влита', queue: 'на проверке', reviewed: 'проверено', nopr: 'нет PR' };
  var LEGEND_ORDER = ['merged', 'queue', 'reviewed', 'nopr'];

  var matrixCells = {},
    studentSet = {},
    labSet = {};
  commits.forEach(function (c) {
    // пустые серии несут только статус лабы
    var commits_ = c.p.length,
      added = 0,
      deleted = 0;
    c.p.forEach(function (pt) {
      added += pt[5];
      deleted += pt[6] || 0;
    });
    matrixCells[c.s + '|' + c.b] = {
      status: c.st || '',
      commits: commits_,
      added: added,
      deleted: deleted,
      url: commits_ ? c.p[commits_ - 1][2] : ''
    };
    studentSet[c.s] = 1;
    labSet[c.b] = 1;
  });
  var matrixLabs = Object.keys(labSet).sort();
  var matrixStudents = Object.keys(studentSet).sort(function (a, b) {
    // успевающие вверху: по числу лаб, затем по имени
    var labsA = 0,
      labsB = 0;
    matrixLabs.forEach(function (lab) {
      if (matrixCells[a + '|' + lab]) labsA++;
      if (matrixCells[b + '|' + lab]) labsB++;
    });
    return labsB - labsA || (a < b ? -1 : 1);
  });

  var rowAlpha = {},
    colAlpha = {},
    matrixReq = 0,
    matrixGeom = null,
    matrixHover = null;

  function rowTarget(name) {
    // подсветка строки: наведение сильнее закрепления из списка
    var anyPicked = Object.keys(picked).length > 0;
    if (hover.student) return hover.student === name ? 1 : 0.12;
    return !anyPicked || picked[name] ? 1 : 0.12;
  }
  function colTarget(lab) {
    return !labSelect.value || lab === labSelect.value ? 1 : 0.15;
  }

  function drawMatrix() {
    cancelAnimationFrame(matrixReq);
    var t0 = performance.now();
    var rowsFrom = {},
      rowsTo = {},
      colsFrom = {},
      colsTo = {};
    matrixStudents.forEach(function (s) {
      rowsFrom[s] = s in rowAlpha ? rowAlpha[s] : rowTarget(s);
      rowsTo[s] = rowTarget(s);
    });
    matrixLabs.forEach(function (lab) {
      colsFrom[lab] = lab in colAlpha ? colAlpha[lab] : colTarget(lab);
      colsTo[lab] = colTarget(lab);
    });
    (function step(now) {
      var k = Math.min(1, (now - t0) / 100),
        eased = k * (2 - k),
        rows = {},
        cols = {};
      matrixStudents.forEach(function (s) {
        rows[s] = rowsFrom[s] + (rowsTo[s] - rowsFrom[s]) * eased;
      });
      matrixLabs.forEach(function (lab) {
        cols[lab] = colsFrom[lab] + (colsTo[lab] - colsFrom[lab]) * eased;
      });
      rowAlpha = rows;
      colAlpha = cols;
      matrixPaint();
      if (k < 1) matrixReq = requestAnimationFrame(step);
    })(t0);
  }

  // Обрезка подписей: measureText дорога, а шрифт постоянен — результаты кэшируются.
  var labelCache = {}; // подпись узла графа: урезанное имя без «…»

  function shortLabel(ctx, name) {
    var cached = labelCache[name];
    if (cached === undefined) {
      cached = name;
      while (cached.length > 2 && ctx.measureText(cached + '…').width > 110) cached = cached.slice(0, -1);
      labelCache[name] = cached;
    }
    return cached;
  }

  // Подпись строки матрицы: урезанное имя с «…» (кэш — по имени и ширине поля).
  function rowLabel(ctx, name, limit) {
    var key = name + '@' + limit;
    var cached = labelCache[key];
    if (cached === undefined) {
      cached = name;
      while (cached.length > 2 && ctx.measureText(cached + '…').width > limit) cached = cached.slice(0, -1);
      labelCache[key] = cached;
    }
    return cached === name ? name : cached + '…';
  }

  function matrixPaint() {
    var box = matrixCanvas.parentElement,
      W = box.clientWidth,
      H = box.clientHeight;
    if (!W || !H) return;
    var dpr = window.devicePixelRatio || 1;
    if (matrixCanvas.width !== W * dpr || matrixCanvas.height !== H * dpr) {
      matrixCanvas.width = W * dpr;
      matrixCanvas.height = H * dpr;
    }
    var g = matrixCanvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);

    var muted = cssVar('--muted', '#57606a');
    var dark = bodyIsDark();
    var emptyColor = dark ? '#161b22' : '#ebedf0';
    var rows = matrixStudents.length,
      cols = matrixLabs.length;
    var TOP = 24,
      BOTTOM = 36;
    g.font = '11px system-ui, sans-serif';
    g.textBaseline = 'middle';

    var labelWidth = 96;
    for (var i = 0; i < rows; i++) labelWidth = Math.max(labelWidth, Math.min(150, g.measureText(matrixStudents[i]).width + 14));
    var cw = (W - labelWidth - 8) / cols,
      rh = (H - TOP - BOTTOM) / rows;
    matrixGeom = { LW: labelWidth, cw: cw, rh: rh, TOP: TOP };
    matrixInfo.style.left = labelWidth + 6 + 'px'; // инфо начинается правее имён, как сетка

    g.fillStyle = muted;
    g.textAlign = 'center';
    for (var c = 0; c < cols; c++) {
      g.fillText(cw > 64 ? matrixLabs[c] : matrixLabs[c].replace('lab-', 'л'), labelWidth + cw * (c + 0.5), TOP / 2);
    }

    for (var r = 0; r < rows; r++) {
      var student = matrixStudents[r],
        rAlpha = student in rowAlpha ? rowAlpha[student] : 1;
      g.globalAlpha = rAlpha;
      g.fillStyle = muted;
      g.textAlign = 'right';
      g.fillText(rowLabel(g, student, labelWidth - 12), labelWidth - 6, TOP + rh * (r + 0.5));
      for (var col = 0; col < cols; col++) {
        var cell = matrixCells[student + '|' + matrixLabs[col]];
        var status = cell ? cell.status : '';
        g.globalAlpha = rAlpha * (matrixLabs[col] in colAlpha ? colAlpha[matrixLabs[col]] : 1);
        var color = cell && status ? STATUS_COLOR[status] || emptyColor : emptyColor;
        g.fillStyle = color;
        g.fillRect(labelWidth + cw * col + 1, TOP + rh * r + 1, cw - 2, rh - 2);
        if (!cell || !cell.commits) continue;
        g.fillStyle = readableText(color, dark);
        g.textAlign = 'center';
        var text = cell.added || cell.deleted ? cell.commits + ' (+' + cell.added + '/−' + cell.deleted + ')' : String(cell.commits);
        if (g.measureText(text).width > cw - 8) text = String(cell.commits); // не влезает — оставляем число коммитов
        g.fillText(text, labelWidth + cw * (col + 0.5), TOP + rh * (r + 0.5));
      }
    }
    g.globalAlpha = 1;

    // Легенда статусов — над инфо-строкой.
    var legendX = labelWidth,
      legendY = H - BOTTOM + 8;
    g.font = '10px system-ui, sans-serif';
    LEGEND_ORDER.forEach(function (status) {
      g.fillStyle = STATUS_COLOR[status];
      g.fillRect(legendX, legendY - 5, 10, 10);
      g.fillStyle = muted;
      g.textAlign = 'left';
      g.fillText(STATUS_TEXT[status], legendX + 14, legendY + 1);
      legendX += 20 + g.measureText(STATUS_TEXT[status]).width;
    });

    if (matrixHover) {
      g.strokeStyle = '#4493f8';
      g.lineWidth = 2;
      g.strokeRect(labelWidth + matrixHover.col * cw + 1.5, TOP + matrixHover.row * rh + 1.5, cw - 3, rh - 3);
    }
  }

  matrixCanvas.addEventListener('mousemove', function (e) {
    if (!matrixGeom) return;
    var rect = matrixCanvas.getBoundingClientRect();
    var col = Math.floor((e.clientX - rect.left - matrixGeom.LW) / matrixGeom.cw);
    var row = Math.floor((e.clientY - rect.top - matrixGeom.TOP) / matrixGeom.rh);
    var cell = col >= 0 && col < matrixLabs.length && row >= 0 && row < matrixStudents.length ? { row: row, col: col } : null;
    var same = !!(cell && matrixHover && cell.row === matrixHover.row && cell.col === matrixHover.col);
    if (!same && (cell || matrixHover)) {
      matrixHover = cell;
      matrixPaint();
    }
    if (!cell) {
      matrixInfo.textContent = MATRIX_HINT;
      return;
    }
    var student = matrixStudents[cell.row],
      lab = matrixLabs[cell.col];
    var data = matrixCells[student + '|' + lab];
    if (!data) {
      matrixInfo.textContent = student + ' · ' + lab + ' — ещё не брался';
      return;
    }
    matrixInfo.textContent =
      student +
      ' · ' +
      lab +
      ' — ' +
      (STATUS_TEXT[data.status] || 'статус не определён') +
      (data.commits ? ', ' + data.commits + ' комм.' : '') +
      (data.added || data.deleted ? ', +' + data.added + '/−' + data.deleted : '');
  });
  matrixCanvas.addEventListener('mouseleave', function () {
    matrixHover = null;
    if (mode === 'matrix') {
      matrixPaint();
      matrixInfo.textContent = MATRIX_HINT;
    }
  });
  matrixCanvas.addEventListener('click', function () {
    // клик по ячейке — последний коммит лабы
    if (!matrixHover) return;
    var cell = matrixCells[matrixStudents[matrixHover.row] + '|' + matrixLabs[matrixHover.col]];
    if (cell && cell.url) window.open(cell.url, '_blank');
  });

  // ═══════════════ 10. ⚖️ JPlag: гистограмма и граф сходства ═══════════════

  var HIST_LABELS = ['90–100%', '80–90%', '70–80%', '60–70%', '50–60%', '40–50%', '30–40%', '20–30%', '10–20%', '0–10%'];
  var GRAPH_TOP_EDGES = 18; // рёбер в графе: топ по AVG, все пары — одуванчик вместо смысла
  var GRAPH_HINT =
    'узел — студент, ребро — похожесть пары (топ-' + GRAPH_TOP_EDGES + ' по AVG); наведение — детали, клик по узлу — закрепить';

  var histChart = null; // ленивая: Chart.js измеряет контейнер при создании, а hidden-контейнер нулевой
  var graphPinned = null; // студент, закреплённый кликом по узлу
  var graphHover = null; // {node:i} | {edge:i} — что под курсором графа
  var graphFrame = null; // последний кадр {W,H,nodes,edges,pairs} для хит-тестов
  var graphMinAvg = 0; // бегунок #jb-min-edge: рёбра слабее порога не рисуем
  var graphFrameReq = 0; // перерисовка не чаще кадра при перетаскивании бегунка
  var pairsCache = null; // кэш jplagPairs(): ключ — лаба+закреплённые, порог ключ не меняет

  function pairsWord(n) {
    if (n % 10 === 1 && n % 100 !== 11) return 'пара';
    if (n % 10 >= 2 && n % 10 <= 4 && !(n % 100 >= 12 && n % 100 <= 14)) return 'пары';
    return 'пар';
  }

  // Пары выбранной лабы (пусто = все), по убыванию AVG. Если слева закреплены
  // студенты — только пары с их участием: контрагенты остаются и показывают,
  // с кем похож закреплённый.
  function jplagPairs() {
    var key = labSelect.value + '|' + Object.keys(picked).sort().join(',');
    if (pairsCache && pairsCache.key === key) return pairsCache.pairs; // результат только читают
    var anyPicked = Object.keys(picked).length > 0;
    var pickedMap = {};
    Object.keys(picked).forEach(function (name) {
      pickedMap[name] = 1;
    });
    var pairs = [];
    if (!jplag) return pairs;
    jplag.labs.forEach(function (lab) {
      if (labSelect.value && lab.lab !== labSelect.value) return;
      lab.pairs.forEach(function (p) {
        if (anyPicked && !pickedMap[p.a] && !pickedMap[p.b]) return;
        pairs.push({ a: p.a, b: p.b, avg: p.avg, max: p.max, lab: lab.lab });
      });
    });
    pairs.sort(function (x, y) {
      return y.avg - x.avg;
    });
    pairsCache = { key: key, pairs: pairs };
    return pairs;
  }

  function jplagPickedSuffix() {
    var names = Object.keys(picked);
    return names.length ? ' · закреплены: ' + names.sort().join(', ') : '';
  }

  function jplagHint() {
    return GRAPH_HINT + jplagPickedSuffix() + (graphMinAvg > 0 ? ' · рёбра ≥' + graphMinAvg + '%' : '');
  }

  // ── 10а. Гистограмма распределения AVG (Chart.js) ──

  function drawJplagHist() {
    if (!jplag) return;
    var pairs = jplagPairs();
    var buckets = [0, 0, 0, 0, 0, 0, 0, 0, 0, 0];
    pairs.forEach(function (p) {
      buckets[Math.min(9, Math.floor(p.avg / 10))]++;
    });
    buckets.reverse(); // категория 0 у горизонтального Chart.js рисуется сверху

    var muted = cssVar('--muted', '#57606a');
    if (!histChart) {
      histChart = new Chart(histCanvas, {
        type: 'bar',
        data: {
          labels: [],
          datasets: [{ data: [], backgroundColor: '#f85149', hoverBackgroundColor: '#ff7b72', borderWidth: 0, barPercentage: 0.8 }]
        },
        options: {
          indexAxis: 'y',
          responsive: true,
          maintainAspectRatio: false,
          animation: { duration: 120 },
          onClick: function () {}, // клик по столбцу ничего не фильтрует: таблица пар — в markdown над виджетом
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: function (ctx) {
                  return ctx.parsed.x + ' ' + pairsWord(ctx.parsed.x);
                }
              }
            }
          },
          scales: {
            x: {
              ticks: { precision: 0, color: muted },
              grid: { color: cssVar('--border', '#d0d7de') },
              title: { display: true, text: 'пар в бакете', color: muted }
            },
            y: { ticks: { color: muted }, grid: { display: false } }
          }
        }
      });
    }
    histChart.data.labels = HIST_LABELS;
    histChart.data.datasets[0].data = buckets;
    histChart.update('none');

    var labNow = labSelect.value || 'все лабы';
    var hasData =
      !labSelect.value ||
      jplag.labs.some(function (lab) {
        return lab.lab === labSelect.value;
      });
    jplagInfo.textContent = !hasData
      ? labNow + ' — нет JPlag-данных'
      : labNow +
        ' · пар ' +
        pairs.length +
        (jplag.checked ? ' · проверка от ' + jplag.checked + ' МСК' : '') +
        ' · полоса — число пар в бакете среднего сходства (AVG)' +
        jplagPickedSuffix();
  }

  // ── 10б. Граф: узлы по окружности, рёбра — топ по AVG ──

  function jplagLayout() {
    var pairs = jplagPairs();
    if (thresholdInput) {
      // потолок бегунка — самый сильный AVG кадра, за него ползунок не вылезет
      var top = pairs.length ? Math.ceil(pairs[0].avg) : 1;
      if (thresholdInput.max != top) thresholdInput.max = top;
      if (graphMinAvg > top) {
        graphMinAvg = top;
        thresholdInput.value = top;
        if (thresholdLabel) thresholdLabel.textContent = '≥' + top + '%';
      }
    }
    if (graphMinAvg > 0)
      pairs = pairs.filter(function (p) {
        return p.avg >= graphMinAvg;
      });

    var seen = {},
      order = [];
    pairs.forEach(function (p) {
      if (!(p.a in seen)) {
        seen[p.a] = 1;
        order.push(p.a);
      }
      if (!(p.b in seen)) {
        seen[p.b] = 1;
        order.push(p.b);
      }
    });
    order.sort(); // порядок детерминированный

    var box = graphCanvas.parentElement,
      W = box.clientWidth,
      H = box.clientHeight;
    if (!W || !H) {
      graphFrame = null;
      return;
    }
    var R = Math.max(60, Math.min(W / 2 - 140, H / 2 - 52, 250)),
      cx = W / 2,
      cy = H / 2 + 6;
    var nodes = order.map(function (name, i) {
      var angle = -Math.PI / 2 + (i * 2 * Math.PI) / Math.max(1, order.length);
      return { name: name, x: cx + R * Math.cos(angle), y: cy + R * Math.sin(angle), angle: angle };
    });
    var index = {};
    order.forEach(function (name, i) {
      index[name] = i;
    });
    var edges = pairs.slice(0, GRAPH_TOP_EDGES).map(function (p) {
      return { a: index[p.a], b: index[p.b], avg: p.avg, max: p.max, lab: p.lab };
    });
    graphFrame = { W: W, H: H, nodes: nodes, edges: edges, pairs: pairs };
  }

  function drawJplagGraph() {
    jplagLayout();
    jplagPaint();
    jplagInfo.textContent = jplagHint();
  }

  // Наведение и клик рисуют одинаковый «фокус»: чужие рёбра и узлы тускнеют.
  function focusStudent() {
    if (graphHover && graphHover.node != null) return graphFrame.nodes[graphHover.node].name;
    return graphPinned;
  }

  function jplagPaint() {
    if (!graphFrame) return;
    var W = graphFrame.W,
      H = graphFrame.H;
    var dpr = window.devicePixelRatio || 1;
    if (graphCanvas.width !== W * dpr || graphCanvas.height !== H * dpr) {
      graphCanvas.width = W * dpr;
      graphCanvas.height = H * dpr;
    }
    var g = graphCanvas.getContext('2d');
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, W, H);
    var muted = cssVar('--muted', '#57606a');
    var fg = cssVar('--fg', '#1f2328');
    var nodes = graphFrame.nodes,
      edges = graphFrame.edges;
    var topAvg = edges.length ? edges[0].avg || 1 : 1;

    var focus = focusStudent();
    var bright = null; // при фокусе ярки только сам студент и соседи по рёбрам
    if (focus) {
      bright = {};
      bright[focus] = 1;
      edges.forEach(function (e) {
        if (nodes[e.a].name === focus) bright[nodes[e.b].name] = 1;
        if (nodes[e.b].name === focus) bright[nodes[e.a].name] = 1;
      });
    }

    var i;
    for (i = 0; i < edges.length; i++) {
      var e = edges[i];
      var related = !focus || nodes[e.a].name === focus || nodes[e.b].name === focus;
      var hovered = graphHover && graphHover.edge === i;
      g.globalAlpha = related ? Math.min(1, 0.25 + 0.7 * (e.avg / topAvg)) : 0.05;
      g.strokeStyle = hovered ? '#4493f8' : '#f85149';
      g.lineWidth = (hovered ? 1.5 : 0) + 0.8 + 2.6 * (e.avg / topAvg);
      g.beginPath();
      g.moveTo(nodes[e.a].x, nodes[e.a].y);
      g.lineTo(nodes[e.b].x, nodes[e.b].y);
      g.stroke();
    }

    g.font = '11px system-ui, sans-serif';
    g.textBaseline = 'middle';
    for (i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      var marked = (graphHover && graphHover.node === i) || node.name === graphPinned;
      g.globalAlpha = bright && !bright[node.name] ? 0.25 : 1;
      g.beginPath();
      g.arc(node.x, node.y, 6, 0, 2 * Math.PI);
      g.fillStyle = 'hsl(' + hueOf(node.name) + ',72%,55%)';
      g.fill();
      if (marked) {
        g.strokeStyle = '#4493f8';
        g.lineWidth = 2;
        g.stroke();
      }
      g.fillStyle = marked ? fg : muted;
      var cos = Math.cos(node.angle);
      g.textAlign = cos > 0.3 ? 'left' : cos < -0.3 ? 'right' : 'center';
      g.fillText(shortLabel(g, node.name), node.x + 11 * cos, node.y + 13 * Math.sin(node.angle));
    }
    g.globalAlpha = 1;
  }

  // Наведение на имя в левом списке — та же подсветка, что от курсора на канвасе.
  function jplagListHover(name) {
    if (!graphCanvas || !graphFrame) return;
    if (name != null) {
      for (var i = 0; i < graphFrame.nodes.length; i++) {
        if (graphFrame.nodes[i].name === name) {
          graphHover = { node: i };
          jplagPaint();
          jplagInfo.textContent = graphInfoText(graphHover);
          return;
        }
      }
    }
    graphHover = null;
    jplagPaint();
    jplagInfo.textContent = jplagHint();
  }

  function distToSegment(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1,
      dy = y2 - y1;
    var lengthSq = dx * dx + dy * dy;
    var t = lengthSq ? ((px - x1) * dx + (py - y1) * dy) / lengthSq : 0;
    t = Math.max(0, Math.min(1, t));
    var qx = x1 + t * dx - px,
      qy = y1 + t * dy - py;
    return Math.sqrt(qx * qx + qy * qy);
  }

  // Сначала узлы (попадание — круг), потом рёбра (±5px).
  function graphHit(x, y) {
    if (!graphFrame) return null;
    var nodes = graphFrame.nodes,
      i;
    for (i = 0; i < nodes.length; i++) {
      var dx = x - nodes[i].x,
        dy = y - nodes[i].y;
      if (dx * dx + dy * dy <= 100) return { node: i };
    }
    for (i = 0; i < graphFrame.edges.length; i++) {
      var e = graphFrame.edges[i];
      if (distToSegment(x, y, nodes[e.a].x, nodes[e.a].y, nodes[e.b].x, nodes[e.b].y) <= 5) return { edge: i };
    }
    return null;
  }

  function graphInfoText(hit) {
    if (!hit || !graphFrame) return jplagHint();
    var nodes = graphFrame.nodes;
    if (hit.node != null) {
      // pairs отсортирован по AVG — первая пара с этим студентом сильнейшая
      var name = nodes[hit.node].name,
        count = 0,
        best = null;
      graphFrame.pairs.forEach(function (p) {
        if (p.a === name || p.b === name) {
          count++;
          if (!best) best = p;
        }
      });
      var text = name + ' — пар ' + count;
      if (best) text += ', максимум ' + best.avg + '% (с ' + (best.a === name ? best.b : best.a) + ', ' + best.lab + ')';
      return text;
    }
    var e = graphFrame.edges[hit.edge];
    return nodes[e.a].name + ' ↔ ' + nodes[e.b].name + ' · AVG ' + e.avg + '% · MAX ' + e.max + '% · ' + e.lab;
  }

  if (graphCanvas) {
    // канвасов может не быть (summary не закоммичен) — слушатели вешаем только когда есть
    graphCanvas.addEventListener('mousemove', function (e) {
      var rect = graphCanvas.getBoundingClientRect();
      var hit = graphHit(e.clientX - rect.left, e.clientY - rect.top);
      var same = !!(hit && graphHover && (hit.node != null ? graphHover.node === hit.node : graphHover.edge === hit.edge));
      if (!same) {
        graphHover = hit;
        jplagPaint();
      }
      graphCanvas.style.cursor = hit ? 'pointer' : 'default';
      jplagInfo.textContent = graphInfoText(hit);
    });
    graphCanvas.addEventListener('mouseleave', function () {
      graphHover = null;
      jplagPaint();
      jplagInfo.textContent = jplagHint();
    });
    graphCanvas.addEventListener('click', function () {
      // узел — закрепить/отпустить, фон — снять
      if (graphHover && graphHover.node != null) {
        var name = graphFrame.nodes[graphHover.node].name;
        graphPinned = graphPinned === name ? null : name;
      } else graphPinned = null;
      jplagPaint();
    });
  }

  if (thresholdInput) {
    // бегунок минимальной похожести: не больше одной перерисовки на кадр
    thresholdInput.oninput = function () {
      graphMinAvg = parseFloat(thresholdInput.value) || 0;
      if (thresholdLabel) thresholdLabel.textContent = '≥' + graphMinAvg + '%';
      if (mode !== 'jb-graph' || graphFrameReq) return;
      graphFrameReq = requestAnimationFrame(function () {
        graphFrameReq = 0;
        drawJplagGraph();
      });
    };
  }
})();
