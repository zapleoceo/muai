"""Скрипт строки связи в карточке «Людей»: собеседник, роли, вес, действия
«Разорвать» и «Это неверно». Склеивается с остальными частями в одном `<script>`:
функции объявлены тут и зовутся оттуда, а `esc` и `predLabel` берутся из общей области.

Кнопки несут только индексы (`data-conn`, `data-role`) в массиве связей текущей
карточки — никаких данных пары в атрибутах, поэтому подмена разметки не
подставит чужие id в запрос.
"""
from __future__ import annotations

CONNECTIONS_SCRIPT = r"""
function connMeta(c){
  const m = c.main, meta = [];
  if (m.inferred && m.support) meta.push('подтверждено ' + m.support + ' + общение');
  else if (m.support) meta.push(m.support + ' подтв.');
  if (m.manual) meta.push('задано вручную');
  else if (m.inferred && !m.support) meta.push('выведено из общения');
  if (c.interaction.active_days) meta.push(c.interaction.active_days + ' дн. общения');
  return meta.join(' · ');
}

const roleName = r => esc(r.label || predLabel(r.predicate)) +
  (r.inferred ? (r.support ? ' (+ общение)' : ' (выведено)') : '');

function actBtn(act, ci, ri, label, hint){
  return '<button type="button" class="ghost sm g-act" data-act="' + act + '" data-conn="' + ci +
    '" data-role="' + ri + '" title="' + esc(hint) + '">' + label + '</button>';
}

function roleLine(c, ci, ri){
  const r = ri === 0 ? c.main : c.also[ri - 1];
  const acts = [];
  if (r.rel_ids && r.rel_ids.length)
    acts.push(actBtn('break', ci, ri, '✕ Разорвать', 'Погасить связь: она пропадёт из карточки и графа'));
  if (r.inferred)
    acts.push(actBtn('reject', ci, ri, 'Это неверно', 'Не считать общение уликой этой связи'));
  return '<div class="g-role"><span class="chip' + (ri === 0 ? ' on' : '') + '">' + roleName(r) +
    '</span><span class="g-acts">' + acts.join('') + '</span></div>';
}

function connRow(c, ci){
  const same = (c.possible_same || []).map(h =>
    '<a href="#" data-focus="' + h.id + '">' + esc(h.name) + '</a>').join(', ');
  const roles = [roleLine(c, ci, 0), ...c.also.map((_, i) => roleLine(c, ci, i + 1))].join('');
  const w = Math.round(Math.max(0.04, Math.min(1, c.weight)) * 100);
  return '<li class="g-conn" data-other="' + c.other_id + '">' +
    '<div class="g-conn-head"><img src="/entities/' + c.other_id + '/avatar" alt="" loading="lazy">' +
    '<a href="#" data-focus="' + c.other_id + '">' + esc(c.other_name) + '</a>' +
    '<span class="g-weight" title="вес связи ' + c.weight.toFixed(2) + '"><i style="width:' + w + '%"></i></span></div>' +
    roles + '<div class="muted small">' + connMeta(c) + '</div>' +
    (same ? '<div class="muted small">возможно тот же человек: ' + same + '</div>' : '') + '</li>';
}
"""
