"""Скрипт строки связи в боковой панели «Людей»: «Имя — роль (вес · подтверждений) ·
также: …». Склеивается с `graph_script` в одном `<script>`: функции объявлены тут и
зовутся оттуда, а `esc` и `predLabel` берутся из общей области.
"""
from __future__ import annotations

CONNECTIONS_SCRIPT = r"""
function connMeta(c){
  const m = c.main, meta = ['вес ' + c.weight.toFixed(2)];
  if (m.inferred && m.support) meta.push('подтверждено ' + m.support + ' + общение');
  else if (m.support) meta.push(m.support + ' подтв.');
  if (m.manual) meta.push('задано вручную');
  else if (m.inferred && !m.support) meta.push('выведено из общения');
  if (c.interaction.active_days) meta.push(c.interaction.active_days + ' дн. общения');
  return meta.join(' · ');
}

function connRow(c){
  const roleName = r => esc(r.label || predLabel(r.predicate)) +
    (r.inferred ? (r.support ? ' (+ общение)' : ' (выведено из общения)') : '');
  const also = c.also.length ? ' · также: ' + c.also.map(roleName).join(', ') : '';
  const same = (c.possible_same || []).map(h =>
    '<a href="#" data-focus="' + h.id + '">' + esc(h.name) + '</a>').join(', ');
  return '<li><a href="#" data-focus="' + c.other_id + '">' + esc(c.other_name) + '</a> — ' +
    roleName(c.main) + ' <span class="muted small">(' + connMeta(c) + ')</span>' + also +
    (same ? '<div class="muted small">возможно тот же человек: ' + same + '</div>' : '') + '</li>';
}
"""
