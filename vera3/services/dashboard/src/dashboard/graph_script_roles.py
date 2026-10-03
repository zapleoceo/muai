"""Скрипт «Указать связь» в карточке: пара — открытая карточка и якорь (владелец или связь),
роль из `MANUAL_ROLES`; подтверждение и откат — как у остальных правок."""
from __future__ import annotations

ROLES_SCRIPT = r"""
const MANUAL_ROLES = __MANUAL_ROLES__;

// «Указать связь»: пара — открытая карточка и якорь (владелец или одна из её связей).
function addRelationship(){
  const anchors = [];
  if (current.owner_id && current.owner_id !== current.id)
    anchors.push({value: String(current.owner_id), label: (current.owner_name || 'Я') + ' (я)'});
  for (const c of current.connections)
    if (c.other_id !== current.owner_id) anchors.push({value: String(c.other_id), label: c.other_name});
  if (!anchors.length){ VeraUI.toast('Не с кем связывать: у карточки нет собеседников.', {kind: 'err'}); return; }
  const names = Object.fromEntries(anchors.map(a => [a.value, a.label.replace(/ \(я\)$/, '')]));
  const roles = anchor => MANUAL_ROLES.map(r => ({value: r.key,
    label: r.text.replace('{x}', current.name).replace('{y}', names[anchor])}));
  const describe = v => 'Будет записано: ' + roles(v.anchor).find(r => r.value === v.role).label +
    '. Связь получит максимальный вес; вернуть можно в журнале.';
  const viewed = current;
  VeraUI.choose({
    title: 'Указать связь', confirmLabel: 'Указать',
    fields: [{name: 'anchor', label: 'С кем', options: anchors},
             {name: 'role', label: 'Какая связь', options: roles(anchors[0].value)}],
    refresh: v => ({message: describe(v), options: {role: roles(v.anchor)}}),
  }).then(v => {
    if (!v) return;
    VeraUI.post('/api/graph/connection/set', {entity_a: viewed.id, entity_b: Number(v.anchor), role: v.role})
      .then(res => afterEdit('Связь указана', res.audit_ids))
      .catch(err => VeraUI.toast('Не получилось: ' + err.message, {kind: 'err'}));
  });
}
"""
