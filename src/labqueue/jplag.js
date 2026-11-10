(function () {
  // ⚖️ jplag-секция: полосы гистограммы — DOM, поэтому весь интерактив это события элементов;
  // никакого canvas/математики мыши (урок ⚡-виджета). Состояние одно на лабу: выбранный бакет.
  function all(sel) { return Array.prototype.slice.call(document.querySelectorAll(sel)); }
  function plural(n) { // пар/пара/пары
    var m10 = n % 10, m100 = n % 100;
    if (m10 === 1 && m100 !== 11) return 'пара';
    if (m10 >= 2 && m10 <= 4 && !(m100 >= 12 && m100 <= 14)) return 'пары';
    return 'пар';
  }
  var sel = {}; // lab -> бакет или null (фильтр таблицы кликом по полосе)
  all('g.jb-bar').forEach(function (bar) {
    var lab = bar.getAttribute('data-lab');
    var b = +bar.getAttribute('data-b');
    var n = +bar.getAttribute('data-n');
    var box = document.getElementById('jb-' + lab);
    if (!box) return;
    var info = box.querySelector('.jb-info');
    function rows() { return all('tr[data-lab="' + lab + '"][data-b="' + b + '"]'); }
    bar.addEventListener('mouseenter', function () {
      if (info) { // строка под графиком: тот же формат, что в title, но RU-склонение
        info.textContent = lab + ' · ' + (b * 10) + '–' + (b * 10 + 10) + '% · ' + n + ' ' + plural(n);
        info.hidden = false;
      }
      rows().forEach(function (tr) { tr.classList.add('jb-same'); });
    });
    bar.addEventListener('mouseleave', function () {
      if (info) info.hidden = true;
      rows().forEach(function (tr) { tr.classList.remove('jb-same'); });
    });
    bar.addEventListener('click', function () {
      sel[lab] = sel[lab] === b ? null : b; // повторный клик — снять фильтр
      all('g.jb-bar[data-lab="' + lab + '"]').forEach(function (g) { g.classList.remove('jb-sel'); });
      if (sel[lab] !== null) bar.classList.add('jb-sel');
      all('tr[data-lab="' + lab + '"][data-b]').forEach(function (tr) {
        tr.classList.toggle('jb-hide', sel[lab] !== null && +tr.getAttribute('data-b') !== sel[lab]);
      });
    });
  });
  all('tr[data-lab][data-b]').forEach(function (tr) { // обратная связь: строка таблицы подсвечивает свою полосу
    var lab = tr.getAttribute('data-lab'), b = tr.getAttribute('data-b');
    tr.addEventListener('mouseenter', function () {
      var bar = document.querySelector('g.jb-bar[data-lab="' + lab + '"][data-b="' + b + '"]');
      if (bar) bar.classList.add('jb-same');
    });
    tr.addEventListener('mouseleave', function () {
      var bar = document.querySelector('g.jb-bar[data-lab="' + lab + '"][data-b="' + b + '"]');
      if (bar) bar.classList.remove('jb-same');
    });
  });
})();
