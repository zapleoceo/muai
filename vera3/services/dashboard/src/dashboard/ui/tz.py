"""Перевод <time data-utc> в часовой пояс браузера."""
from __future__ import annotations

TZ_FOOTER = '<footer id="tz-note" class="tz-note"></footer>'

# Переводит все <time data-utc> в часовой пояс браузера. Запускается сразу
# (скрипт в конце body — DOM уже готов) и после каждого htmx-swap (live-прогресс
# подменяется каждые 30с). window.__localizeTimes открыт для ручного вызова.
TZ_SCRIPT = """<script>
(function(){
  var M=['января','февраля','марта','апреля','мая','июня','июля','августа','сентября','октября','ноября','декабря'];
  function p(n){return String(n).padStart(2,'0');}
  function sameDay(a,b){return a.getFullYear()===b.getFullYear()&&a.getMonth()===b.getMonth()&&a.getDate()===b.getDate();}
  // Одна форма даты на весь дашборд: «25 мая 2026, 14:03»; недавнее — «сегодня, 14:03», «вчера, 14:03».
  function fmt(d,k){
    var h=p(d.getHours()),m=p(d.getMinutes()),s=p(d.getSeconds());
    if(k==='time')return h+':'+m;
    var day=d.getDate()+' '+M[d.getMonth()]+' '+d.getFullYear();
    if(k==='date'||k==='date_human')return day;
    var now=new Date(),yest=new Date(now.getFullYear(),now.getMonth(),now.getDate()-1);
    var label=sameDay(d,now)?'сегодня':sameDay(d,yest)?'вчера':day;
    return label+', '+h+':'+m+(k==='datetime_sec'?':'+s:'');
  }
  window.__fmtDate=fmt;
  function localize(root){
    (root||document).querySelectorAll('time[data-utc]').forEach(function(el){
      var iso=el.getAttribute('data-utc'),d=new Date(iso);
      if(isNaN(d.getTime()))return;
      el.textContent=fmt(d,el.getAttribute('data-fmt')||'datetime');
      el.title='UTC: '+iso;
    });
    var tz=document.getElementById('tz-note');
    if(tz&&!tz.dataset.done){
      // Offset — единственное, что реально определяет показанное время.
      // Название зоны (Asia/Bangkok и т.п.) берётся из настроек ОС/браузера
      // и может отличаться от твоего города при том же offset — города UTC+7
      // (Джакарта, Бангкок, Хошимин) показывают ОДНО И ТО ЖЕ время.
      var off=-new Date().getTimezoneOffset();
      var oh=Math.floor(Math.abs(off)/60),om=Math.abs(off)%60;
      var offStr='UTC'+(off>=0?'+':'-')+oh+(om?':'+p(om):'');
      var zone='';
      try{zone=Intl.DateTimeFormat().resolvedOptions().timeZone;}catch(e){}
      tz.textContent='Время — в вашем часовом поясе, '+offStr+(zone?' ('+zone+')':'');
      tz.dataset.done='1';
    }
  }
  window.__localizeTimes=localize;
  localize();
  document.body.addEventListener('htmx:afterSwap',function(e){localize(e.target);});
})();
</script>"""

# Заголовки дней — по местному времени браузера: серверный день в UTC у
# владельца в UTC+7 переворачивался бы в 07:00. Без JS остаются серверные
# заголовки (tr.day-fb), с JS они заменяются.
DAYS_SCRIPT = """<script>
(function(){
  function p(n){return String(n).padStart(2,'0');}
  function key(d){return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());}
  var now=new Date(),ty=new Date(now.getFullYear(),now.getMonth(),now.getDate()-1);
  function label(d){
    var k=key(d);
    if(k===key(now))return 'Сегодня';
    if(k===key(ty))return 'Вчера';
    return p(d.getDate())+'.'+p(d.getMonth()+1)+'.'+d.getFullYear();
  }
  document.querySelectorAll('table.data').forEach(function(t){
    var rows=t.querySelectorAll('tr.ev[data-utc]');
    if(!rows.length)return;
    t.querySelectorAll('tr.day-fb').forEach(function(r){r.remove();});
    var prev='';
    rows.forEach(function(r){
      var d=new Date(r.getAttribute('data-utc'));
      if(isNaN(d.getTime()))return;
      var k=key(d);
      if(k===prev)return;
      prev=k;
      var h=document.createElement('tr'),c=document.createElement('td');
      c.colSpan=r.children.length;c.className='day';c.textContent=label(d);
      h.appendChild(c);r.parentNode.insertBefore(h,r);
    });
  });
})();
</script>"""
