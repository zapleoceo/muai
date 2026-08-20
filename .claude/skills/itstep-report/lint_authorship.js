#!/usr/bin/env node
// Линтер атрибуций в отчёте.
//
// Зачем: правило «проверь, кто кому написал» пять раз подряд оказывалось
// пропущенным под конец работы. Проверка должна быть механической, а не
// дисциплинарной.
//
// Использование:
//   1) выгрузить окно отчёта:
//      ssh hetzner-root "docker exec -i vera3-postgres psql -U vera -d vera -t -A -F'\t' \
//        -c \"SELECT direction, speaker, addressee, peer_key, line FROM v_message_lines \
//             WHERE ts_utc > TIMESTAMP '<cutoff>' ORDER BY ts_utc\"" > window.tsv
//   2) node lint_authorship.js window.tsv draft.md
//
// Выход: таблица по каждому имени и список подозрительных предложений.
// Ненулевой код возврата = отчёт отдавать нельзя.

const fs = require('fs');
const path = require('path');

const [, , windowFile, draftFile] = process.argv;
if (!windowFile || !draftFile) {
  console.error('usage: node lint_authorship.js <window.tsv> <draft.md>');
  process.exit(2);
}

const people = JSON.parse(fs.readFileSync(path.join(__dirname, 'people.json'), 'utf8'));
const aliasOf = {};
for (const [name, aliases] of Object.entries(people)) {
  if (name.startsWith('_')) continue;
  // само имя тоже алиас: в исходящих addressee это chat_title, а он часто
  // совпадает с тем, как человек назван в отчёте.
  for (const a of [name, ...aliases]) aliasOf[a.toLowerCase()] = name;
}
const AMBIGUOUS = new Set(Object.keys(people._ambiguous_titles || {}).filter(k => !k.startsWith('_')));

// --- окно ---
const rows = fs.readFileSync(windowFile, 'utf8')
  .split('\n').filter(Boolean)
  .map(l => {
    const [direction, speaker, addressee, peer, ...rest] = l.split('\t');
    return { direction, speaker, addressee, peer, line: rest.join('\t') };
  });

// сколько раз человек РЕАЛЬНО говорил (in) и сколько раз писали ЕМУ (out)
const stat = {};
const touch = n => (stat[n] = stat[n] || { in: 0, out: 0, peers: new Set() });
for (const r of rows) {
  const sp = aliasOf[(r.speaker || '').toLowerCase()];
  const ad = aliasOf[(r.addressee || '').toLowerCase()];
  if (r.direction === 'in' && sp) { touch(sp).in++; touch(sp).peers.add(r.peer); }
  if (r.direction === 'out' && ad) { touch(ad).out++; touch(ad).peers.add(r.peer); }
}

// --- черновик ---
const draft = fs.readFileSync(draftFile, 'utf8')
  .split('\n').filter(l => !l.startsWith('#')).join(' ');
const sentences = draft.split(/(?<=[.!?])\s+/).map(s => s.trim()).filter(Boolean);

// глаголы речи и действия: только они делают имя подлежащим-говорящим
// ВНИМАНИЕ: без \b и \w — в JS они работают только по латинице, поэтому
// кириллическая регулярка с \b не срабатывает никогда. Ровно на этом
// линтер молча пропускал ошибки при первой сборке.
const VERB = /(сказал|написал|прислал|ответил|сообщил|предложил|попросил|подтвердил|возразил|спросил|передал|уточнил|назвал|поручил|отчитал|заметил|объяснил|высказал|отправил|показал|согласовал|запустил|нашёл|нашла|дал|дала|решил|признал|пожаловал)[а-яё]*/i;

// Сопоставление по содержанию, а не по факту «человек в окне говорил».
// Именно этого не хватило 11 августа: Алексей в тот день писал много, но
// конкретную реплику про созвон писал ДИМА ему, а не наоборот.
const STOP = new Set(['который','которая','которые','потому','поэтому','сегодня','вчера','сейчас','нужно','надо','можно','будет','было','этот','этом','этого','когда','чтобы','после','перед','через','около','более','менее','также','ещё','его','её','их','что','как','для','про','без']);
// Префиксный стемминг: «проблема» и «проблемой» должны совпасть, иначе
// сопоставление разваливается на падежах.
const toks = s => [...new Set(
  (s.toLowerCase().match(/[a-zа-яё0-9]{5,}/g) || [])
    .filter(w => !STOP.has(w))
    .map(w => w.slice(0, 6))
)];

const linesIn = {}, linesOut = {};
for (const r of rows) {
  const sp = aliasOf[(r.speaker || '').toLowerCase()];
  const ad = aliasOf[(r.addressee || '').toLowerCase()];
  if (r.direction === 'in' && sp) (linesIn[sp] = linesIn[sp] || []).push(r.line);
  if (r.direction === 'out' && ad) (linesOut[ad] = linesOut[ad] || []).push(r.line);
}
const best = (arr, t) => {
  let bs = 0, bl = '';
  for (const l of arr || []) {
    const lt = new Set(toks(l));
    const n = t.filter(w => lt.has(w)).length;
    if (n > bs) { bs = n; bl = l; }
  }
  return { score: bs, line: bl };
};

const problems = [];
const unknown = new Set();

for (const s of sentences) {
  for (const name of Object.keys(people)) {
    if (name.startsWith('_')) continue;
    const re = new RegExp('(^|[^A-Za-zА-Яа-яЁё])' + name + '([^A-Za-zА-Яа-яЁё]|$)');
    if (!re.test(s)) continue;
    const idx = s.search(re);
    if (!VERB.test(s.slice(idx, idx + 160))) continue;   // имя есть, но не как говорящий
    const st = stat[name];
    if (!st || (st.in === 0 && st.out === 0)) { unknown.add(name); continue; }

    const t = toks(s);
    const bi = best(linesIn[name], t);
    const bo = best(linesOut[name], t);
    // сказанное приписано человеку, но текстуально совпадает с тем, что
    // ДИМА писал ЕМУ — значит подлежащее перевёрнуто
    if (bo.score >= 3 && bo.score > bi.score) {
      problems.push({ name, sentence: s, bi, bo, st });
    } else if (st.in === 0) {
      problems.push({ name, sentence: s, bi, bo, st });
    }
  }
}

// --- вывод ---
const pad = (s, n) => String(s).padEnd(n);
console.log('АУДИТ АТРИБУЦИЙ\n');
console.log(pad('имя', 14) + pad('говорил', 9) + pad('писали ему', 12) + 'чаты');
console.log('-'.repeat(70));
for (const [name, st] of Object.entries(stat).sort((a, b) => b[1].in + b[1].out - a[1].in - a[1].out)) {
  console.log(pad(name, 14) + pad(st.in, 9) + pad(st.out, 12) + [...st.peers].join(', ').slice(0, 60));
}

let bad = 0;
const ambig = Object.entries(stat)
  .filter(([n, st]) => AMBIGUOUS.has(n) && st.peers.size > 1);
if (ambig.length) {
  console.log('\n⚠ НЕОДНОЗНАЧНОЕ ИМЯ — в окне за ним больше одного чата.');
  console.log('  Убедись, что в отчёте имеется в виду нужный человек:');
  for (const [n, st] of ambig) console.log('  - ' + n + ': ' + [...st.peers].join(' | '));
  bad++;
}
if (unknown.size) {
  console.log('\n⚠ В ОТЧЁТЕ ЕСТЬ, В ОКНЕ НЕТ (проверь, не выдумано ли):');
  for (const n of unknown) console.log('  - ' + n);
  bad++;
}
if (problems.length) {
  console.log('\n✗ ВЕРОЯТНЫЙ ПЕРЕВОРОТ АВТОРСТВА');
  for (const p of problems) {
    console.log('\n  ' + p.name + ' — говорил ' + p.st.in + ', писали ему ' + p.st.out);
    console.log('  в отчёте: «' + p.sentence.slice(0, 200) + '»');
    if (p.bo.line) console.log('  ДИМА --> ' + p.name + ' (совпадений ' + p.bo.score + '): ' + p.bo.line.slice(0, 180));
    if (p.bi.line) console.log('  ' + p.name + ' --> ДИМА (совпадений ' + p.bi.score + '): ' + p.bi.line.slice(0, 180));
    else console.log('  ' + p.name + ' --> ДИМА: ничего похожего не найдено');
  }
  bad++;
}
if (!bad) console.log('\n✓ переворотов не найдено');
process.exit(bad ? 1 : 0);
