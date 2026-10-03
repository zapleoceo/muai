"""Скрипт «Это тот же человек…» и «Связи не про этого человека» в карточке «Людей».

Два сценария в одном диалоге: найти человека → посмотреть, что произойдёт (предпросмотр
ничего не пишет) → подтвердить → тост «Вернуть» (откат через журнал). Разница в одной
строке: объединить — один человек с двумя аккаунтами; перенести связи — аккаунт чужой,
а связи про другого. Всё из БД экранируется `esc`.
"""
from __future__ import annotations

MERGE_SCRIPT = r"""
const MERGE_COPY = 'Объединить — это один человек с двумя аккаунтами. Перенести связи — аккаунт чужой, а связи про другого.';
const COUNT_RU = {entity_aliases_moved: 'алиасов', relationships_moved: 'связей', relationships_deleted: 'дублей связей склеится',
  memberships_moved: 'участий в группах', memberships_deleted: 'дублей участий склеится', entity_avatars_moved: 'фото'};
const mergeDlg = $('merge-dlg'), mergeBody = $('merge-body');
let mergeMode = null, mergeFrom = null, mergePick = null, mergeKeep = null, mergeRows = [];

const personLine = p => '<div class="pcard"><img src="/entities/' + p.id + '/avatar" alt=""><div><b>' + esc(p.name) + '</b>' +
  '<div class="muted small">' + [p.username ? '@' + esc(p.username) : '', p.email ? esc(p.email) : '',
  p.last_seen_at ? 'активность ' + esc(fmtStamp(p.last_seen_at)) : ''].filter(Boolean).join(' · ') + '</div>' +
  '<div class="muted small">' + p.aliases + ' алиасов · ' + p.relationships + ' связей · ' + p.groups + ' групп</div></div></div>';

function mergeShell(title, intro, confirmLabel){
  mergeBody.innerHTML = '<h3>' + esc(title) + '</h3><p class="muted">' + esc(intro) + '</p><p class="note-line">' + esc(MERGE_COPY) + '</p>' +
    '<label for="merge-q">Найти человека</label><input id="merge-q" type="search" autocomplete="off" placeholder="Имя, @username или email">' +
    '<ul id="merge-results" class="pick" hidden></ul><div id="merge-preview"></div>' +
    '<div class="dlg-actions"><button type="button" class="secondary" id="merge-cancel">Отмена</button>' +
    '<button type="button" id="merge-go" disabled>' + esc(confirmLabel) + '</button></div>';
  $('merge-cancel').onclick = () => mergeDlg.close();
  $('merge-q').addEventListener('input', searchPeople);
  $('merge-results').addEventListener('click', ev => { const li = ev.target.closest('li'); if (li) pickPerson(Number(li.dataset.id)); });
}

let searchTimer = null, foundPeople = [];
function searchPeople(){
  clearTimeout(searchTimer);
  const q = $('merge-q').value.trim();
  searchTimer = setTimeout(() => {
    if (q.length < 2){ $('merge-results').hidden = true; return; }
    getJson('/api/graph/people/search?q=' + encodeURIComponent(q) + '&exclude=' + mergeFrom.id).then(d => {
      foundPeople = d.people;
      const ul = $('merge-results');
      ul.hidden = !foundPeople.length;
      ul.innerHTML = foundPeople.map(p => '<li data-id="' + p.id + '">' + personLine(p) + '</li>').join('');
    });
  }, 180);
}

function previewError(err){ $('merge-preview').innerHTML = '<p class="err">' + esc(err.message || err) + '</p>'; $('merge-go').disabled = true; }

function pickPerson(id){
  mergePick = foundPeople.find(p => p.id === id);
  $('merge-results').hidden = true;
  $('merge-q').value = mergePick.name;
  $('merge-preview').innerHTML = '<div class="skel-stack"><div class="skeleton"></div><div class="skeleton"></div></div>';
  if (mergeMode === 'merge') previewMerge(null); else previewMove();
}

function previewMerge(keepId){
  VeraUI.post('/api/graph/merge/preview', {a: mergeFrom.id, b: mergePick.id, keep_id: keepId}).then(p => {
    mergeKeep = p;
    const counts = Object.keys(p.counts || {}).filter(k => COUNT_RU[k]).map(k => '<li><b>' + p.counts[k] + '</b> ' + COUNT_RU[k] + '</li>').join('');
    $('merge-preview').innerHTML = '<div class="mcols"><div><h4>Останется главной</h4>' + personLine(p.keep) + '</div>' +
      '<div><h4>Вольётся</h4>' + personLine(p.drop) + '</div></div>' +
      '<button type="button" class="ghost sm" id="merge-swap">⇄ Поменять местами</button>' +
      (p.blockers.length ? '<p class="warning">' + p.blockers.map(esc).join('; ') + ' — из дашборда не объединяется.</p>'
        : '<h4>Что переедет в главную</h4><ul class="g-list">' + (counts || '<li class="muted">ничего, кроме карточки</li>') + '</ul>');
    $('merge-swap').onclick = () => previewMerge(p.drop.id);
    $('merge-go').disabled = p.would_be_refused;
  }).catch(previewError);
}

function previewMove(){
  VeraUI.post('/api/graph/move/preview', {from_id: mergeFrom.id, to_id: mergePick.id}).then(p => {
    mergeRows = p.relationships;
    $('merge-preview').innerHTML = '<div class="mcols"><div><h4>Связи не про него</h4>' + personLine(p.from) + '</div>' +
      '<div><h4>Они про</h4>' + personLine(p.to) + '</div></div>' + (mergeRows.length
        ? '<h4>Связи, взятые из упоминаний имени (не из его сообщений)</h4><ul class="g-list">' + mergeRows.map(r =>
          '<li><label class="chk"><input type="checkbox" data-rel="' + r.rel_id + '" checked> ' + esc(r.other_name) + ' — ' + esc(predLabel(r.predicate)) +
          ' <span class="muted small">' + (r.outcome === 'retired' ? '(такая уже есть — склеится)' : '(переедет)') + '</span></label>' +
          (r.fact ? '<div class="muted small">' + esc(r.fact) + '</div>' : '') + '</li>').join('') + '</ul>'
        : '<p class="muted">Связей, основанных на упоминании имени, нет — переносить нечего.</p>');
    $('merge-go').disabled = !mergeRows.length;
    $('merge-preview').onchange = () => { $('merge-go').disabled = !$('merge-preview').querySelector('input:checked'); };
  }).catch(previewError);
}

function finishEdit(message, res, openId){
  mergeDlg.close();
  afterEdit(message, res.audit_ids);
  if (openId) { load(focusParams(openId)).then(() => openPanel(openId)); }
}

function applyMerge(){
  $('merge-go').disabled = true;
  VeraUI.post('/api/graph/merge/apply', {keep_id: mergeKeep.keep.id, drop_id: mergeKeep.drop.id})
    .then(res => finishEdit('Карточки объединены', res, mergeKeep.keep.id)).catch(previewError);
}

function applyMove(){
  const ids = [...$('merge-preview').querySelectorAll('input[data-rel]:checked')].map(i => Number(i.dataset.rel));
  $('merge-go').disabled = true;
  VeraUI.post('/api/graph/move/apply', {from_id: mergeFrom.id, to_id: mergePick.id, rel_ids: ids})
    .then(res => finishEdit('Связей перенесено: ' + ids.length, res, mergeFrom.id)).catch(previewError);
}

function openMergeDialog(mode){
  mergeMode = mode; mergeFrom = current; mergePick = null;
  if (mode === 'merge') mergeShell('Это тот же человек…', 'Найдите вторую карточку — Вера покажет, что переедет, до того как что-то изменится.', 'Объединить');
  else mergeShell('Связи не про этого человека', 'Выберите, про кого на самом деле эти связи: перенесутся только те, что взяты из упоминаний имени.', 'Перенести связи');
  $('merge-go').onclick = mode === 'merge' ? applyMerge : applyMove;
  mergeDlg.showModal();
  $('merge-q').focus();
}
"""
