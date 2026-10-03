"""Скрипт спойлеров `/sources`: раскрытие строки, ленивая подгрузка подробностей,
состояние открытых строк в `#open=a,b` (перезагрузка и ссылка сохраняют его).
Строка, а не f-string: фигурные скобки JS не экранируются."""
from __future__ import annotations

SOURCES_SCRIPT = r"""
(function(){
  var list = document.getElementById('src-list');
  if (!list) return;
  var items = Array.prototype.slice.call(list.querySelectorAll('.src-item'));

  function openKeys(){
    var m = /(?:^|[#&])open=([^&]*)/.exec(location.hash);
    return m ? decodeURIComponent(m[1]).split(',').filter(Boolean) : [];
  }
  function saveHash(){
    var keys = items.filter(function(i){ return i.classList.contains('open'); })
                    .map(function(i){ return i.dataset.key; });
    history.replaceState(null, '', keys.length ? '#open=' + encodeURIComponent(keys.join(',')) : location.pathname);
  }
  function setOpen(item, open){
    item.classList.toggle('open', open);
    item.querySelector('.src-head').setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open && window.htmx){
      var loader = item.querySelector('.src-load');
      if (loader) htmx.trigger(loader, 'src-open');
    }
  }

  list.addEventListener('click', function(e){
    var head = e.target.closest('.src-head');
    if (!head) return;
    var item = head.parentNode;
    setOpen(item, !item.classList.contains('open'));
    saveHash();
  });
  items.forEach(function(item){
    var m = item.querySelector('.src-body');
    m.addEventListener('htmx:responseError', function(){
      m.querySelector('.src-load').innerHTML = '<p class="err">Не загрузилось. Откройте строку ещё раз.</p>';
      m.querySelector('.src-load').removeAttribute('hx-trigger');
    });
  });

  var wanted = openKeys();
  items.forEach(function(item){ if (wanted.indexOf(item.dataset.key) >= 0) setOpen(item, true); });
  // htmx может быть ещё не готов в момент разбора страницы — досылаем триггер по загрузке.
  window.addEventListener('load', function(){
    items.forEach(function(item){ if (item.classList.contains('open')) setOpen(item, true); });
  });
})();
"""
