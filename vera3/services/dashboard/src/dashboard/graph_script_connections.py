"""Скрипт строки связи в карточке «Людей»: собеседник, роли, вес и ОДНА кнопка
«Изменить» на связь (разорвать роль или отметить выведенную неверной).
Склеивается с остальными частями в одном `<script>`: функции объявлены тут и
зовутся оттуда, а `esc` и `predLabel` берутся из общей области.

Кнопка несёт только индекс связи (`data-conn`) в массиве текущей карточки —
никаких данных пары в атрибутах, поэтому подмена разметки не подставит чужие id.
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
const connRoles = c => [c.main, ...c.also];
const editable = r => (r.rel_ids && r.rel_ids.length) || r.inferred;

function connRow(c, ci){
  const same = (c.possible_same || []).map(h =>
    '<a href="#" data-focus="' + h.id + '">' + esc(h.name) + '</a>').join(', ');
  const roles = connRoles(c).map((r, ri) =>
    '<span class="chip' + (ri === 0 ? ' on' : '') + '">' + roleName(r) + '</span>').join('');
  const manage = connRoles(c).some(editable)
    ? '<button type="button" class="ghost sm g-act" data-act="manage" data-conn="' + ci +
      '" title="Разорвать связь или отметить неверной" aria-label="Изменить связь">⋯</button>' : '';
  const w = Math.round(Math.max(0.04, Math.min(1, c.weight)) * 100);
  return '<li class="g-conn" data-other="' + c.other_id + '">' +
    '<div class="g-conn-head"><img src="/entities/' + c.other_id + '/avatar" alt="" loading="lazy">' +
    '<a href="#" data-focus="' + c.other_id + '">' + esc(c.other_name) + '</a>' +
    '<span class="g-weight" title="вес связи ' + c.weight.toFixed(2) + '"><i style="width:' + w + '%"></i></span>' +
    manage + '</div><div class="g-roles">' + roles + '</div>' +
    '<div class="muted small">' + connMeta(c) + '</div>' +
    (same ? '<div class="muted small">возможно тот же человек: ' + same + '</div>' : '') + '</li>';
}
"""
