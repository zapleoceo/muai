"""Общий скрипт дашборда: подтверждение в `<dialog>`, тосты, клик по строке
таблицы, горячая клавиша поиска. Строка, а не f-string: фигурные скобки JS не
экранируются. Подключается `defer` на каждой странице как `/ui/vera.js`."""
from __future__ import annotations

UI_JS = r"""(function(){
'use strict';
function el(tag, cls, text){
  var n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

var dlg = null;
// Один диалог на страницу: текст идёт через textContent, так что имена людей
// из Telegram/Gmail в вопросе остаются текстом, а не разметкой.
function confirmDialog(opts){
  if (!dlg){
    dlg = el('dialog', 'dlg');
    dlg.setAttribute('aria-labelledby', 'dlg-title');
    document.body.appendChild(dlg);
  }
  dlg.textContent = '';
  var title = el('h3', '', opts.title || 'Подтвердите действие');
  title.id = 'dlg-title';
  var actions = el('div', 'dlg-actions');
  var no = el('button', 'secondary', opts.cancelLabel || 'Отмена');
  var yes = el('button', opts.danger ? 'danger-solid' : '', opts.confirmLabel || 'Подтвердить');
  no.type = yes.type = 'button';
  actions.append(no, yes);
  dlg.append(title, el('p', '', opts.message || ''), actions);
  return new Promise(function(resolve){
    var done = function(v){ dlg.close(); resolve(v); };
    no.onclick = function(){ done(false); };
    yes.onclick = function(){ done(true); };
    dlg.oncancel = function(ev){ ev.preventDefault(); done(false); };
    dlg.onclick = function(ev){ if (ev.target === dlg) done(false); };
    dlg.showModal();
    (opts.danger ? no : yes).focus();
  });
}

// Диалог с выпадающими списками: resolve — {имя: значение} или null. `refresh(values)`
// может вернуть {message, options: {имя: [...]}}: подписи ролей зависят от выбранного человека.
function chooseDialog(opts){
  if (!dlg){
    dlg = el('dialog', 'dlg');
    dlg.setAttribute('aria-labelledby', 'dlg-title');
    document.body.appendChild(dlg);
  }
  dlg.textContent = '';
  var title = el('h3', '', opts.title);
  title.id = 'dlg-title';
  var msg = el('p', '', opts.message || '');
  var selects = {};
  var fill = function(sel, options){
    sel.textContent = '';
    options.forEach(function(o){ var op = el('option', '', o.label); op.value = o.value; sel.appendChild(op); });
  };
  var read = function(){
    var out = {};
    Object.keys(selects).forEach(function(k){ out[k] = selects[k].value; });
    return out;
  };
  dlg.append(title);
  opts.fields.forEach(function(f){
    var label = el('label', '', f.label);
    var sel = el('select');
    fill(sel, f.options);
    selects[f.name] = sel;
    dlg.append(label, sel);
  });
  var actions = el('div', 'dlg-actions');
  var no = el('button', 'secondary', 'Отмена');
  var yes = el('button', opts.danger ? 'danger-solid' : '', opts.confirmLabel || 'Подтвердить');
  no.type = yes.type = 'button';
  actions.append(no, yes);
  dlg.append(msg, actions);
  var refresh = function(){
    if (!opts.refresh) return;
    var r = opts.refresh(read());
    msg.textContent = r.message || '';
    Object.keys(r.options || {}).forEach(function(k){ var v = selects[k].value; fill(selects[k], r.options[k]); selects[k].value = v; });
  };
  Object.keys(selects).forEach(function(k){ selects[k].onchange = refresh; });
  refresh();
  return new Promise(function(resolve){
    var done = function(v){ dlg.close(); resolve(v); };
    no.onclick = function(){ done(null); };
    yes.onclick = function(){ done(read()); };
    dlg.oncancel = function(ev){ ev.preventDefault(); done(null); };
    dlg.onclick = function(ev){ if (ev.target === dlg) done(null); };
    dlg.showModal();
  });
}

var box = null;
function toast(message, opts){
  opts = opts || {};
  if (!box){
    box = el('div'); box.id = 'toasts';
    box.setAttribute('role', 'status'); box.setAttribute('aria-live', 'polite');
    document.body.appendChild(box);
  }
  var t = el('div', 'toast ' + (opts.kind || ''));
  t.appendChild(el('span', '', message));
  var close = function(){ if (t.parentNode) t.remove(); };
  if (opts.action){
    var b = el('button', 'secondary', opts.action.label);
    b.type = 'button';
    b.onclick = function(){ close(); opts.action.run(); };
    t.appendChild(b);
  }
  box.appendChild(t);
  setTimeout(close, opts.ttl || 9000);
}

// Правки идут JSON-запросом со страницы дашборда: Origin/Sec-Fetch-Site браузер ставит сам.
function post(url, body){
  return fetch(url, {method: 'POST', credentials: 'same-origin',
    headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)})
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(j){
        if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status));
        return j;
      });
    });
}

window.VeraUI = {confirm: confirmDialog, choose: chooseDialog, toast: toast, post: post};

document.addEventListener('submit', function(e){
  var f = e.target, msg = f.dataset && f.dataset.confirm;
  if (!msg || f.dataset.confirmed) return;
  e.preventDefault();
  var danger = !!f.querySelector('.danger-solid, .danger');
  confirmDialog({message: msg, danger: danger,
    confirmLabel: (e.submitter && e.submitter.textContent.trim()) || 'Подтвердить'})
    .then(function(ok){
      if (!ok) return;
      f.dataset.confirmed = '1';
      f.requestSubmit(e.submitter || undefined);
    });
});

document.addEventListener('click', function(e){
  var r = e.target.closest('tr.row-link');
  if (r && !e.target.closest('a,button,input,select,summary')) location.href = r.dataset.href;
});

document.addEventListener('keydown', function(e){
  if (e.key !== '/' || e.metaKey || e.ctrlKey || e.altKey) return;
  var t = e.target.tagName;
  if (t === 'INPUT' || t === 'TEXTAREA' || t === 'SELECT') return;
  var f = document.querySelector('[data-hotkey-search]');
  if (f){ e.preventDefault(); f.focus(); }
});
})();
"""
