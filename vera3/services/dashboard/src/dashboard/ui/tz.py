"""Перевод <time data-utc> в часовой пояс браузера."""
from __future__ import annotations

TZ_FOOTER = (
    '<div id="tz-note" class="mute" '
    'style="margin-top:28px;font-size:11px;text-align:center"></div>'
)

# Переводит все <time data-utc> в часовой пояс браузера. Запускается сразу
# (скрипт в конце body — DOM уже готов) и после каждого htmx-swap (live-прогресс
# подменяется каждые 30с). window.__localizeTimes открыт для ручного вызова.
TZ_SCRIPT = """<script>
(function(){
  var M=['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  function p(n){return String(n).padStart(2,'0');}
  function fmt(d,k){
    var Y=d.getFullYear(),Mo=p(d.getMonth()+1),D=p(d.getDate());
    var h=p(d.getHours()),m=p(d.getMinutes()),s=p(d.getSeconds());
    if(k==='time')return h+':'+m;
    if(k==='date')return Y+'-'+Mo+'-'+D;
    if(k==='date_human')return d.getDate()+' '+M[d.getMonth()]+' '+Y;
    if(k==='datetime_sec')return Y+'-'+Mo+'-'+D+' '+h+':'+m+':'+s;
    return Y+'-'+Mo+'-'+D+' '+h+':'+m;
  }
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
      tz.textContent='🕐 время показано в вашем часовом поясе — '+offStr+
        (zone?' (по данным браузера: '+zone+')':'');
      tz.dataset.done='1';
    }
  }
  window.__localizeTimes=localize;
  localize();
  document.body.addEventListener('htmx:afterSwap',function(e){localize(e.target);});
})();
</script>"""
