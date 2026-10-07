"""Credit-plan controls and a separate forecast layer for the dashboard.

The quota chart remains a measured percentage chart. This module never edits its
SVG or its quota readings; the forecast is rendered beside each matching card.
"""
from __future__ import annotations

import html as _html
import json
from datetime import datetime
from typing import Any


def _esc(value: object) -> str:
    return _html.escape(str(value), quote=True)


def _local_input(value: str | datetime | None) -> str:
    if not value:
        return ""
    try:
        date = value if isinstance(value, datetime) else datetime.fromisoformat(value.replace("Z", "+00:00"))
        return date.astimezone().strftime("%Y-%m-%dT%H:%M")
    except (TypeError, ValueError, OverflowError):
        return ""


def _script_json(value: object) -> str:
    # JSON in a script element must not be allowed to close the element.
    return json.dumps(value, ensure_ascii=True, separators=(",", ":")).replace("<", "\\u003c")


def html(snapshot: dict[str, Any] | None, now: datetime, live: bool) -> str:
    """Render persistent controls outside #dashboard-data.

    Server values are initial display only. Live mode refreshes from /v1/plans;
    static mode explains that a saved plan must be changed on the live service.
    """
    snapshot = snapshot or {}
    plans = snapshot.get("plans") or {}
    models = snapshot.get("models") or {}
    start_default = _local_input(now)
    parts = ['<section class="credit-plans" id="credit-plans" aria-labelledby="credit-plans-title">',
             '<h2 id="credit-plans-title">Paid-credit spending plans</h2>',
             '<p class="note">Plans target paid credits spent. Included subscription quota and free limit-reset credits are separate. '
             'Model choice estimates capacity; it does not change the model your agents use.</p>']
    if not live:
        parts.append('<p class="note" role="status">Static report: open the live dashboard to save or clear a plan.</p>')
    parts.append('<div class="credit-plan-grid">')
    for provider, title, unit in (("codex", "Codex", "credits"), ("claude", "Claude", "USD")):
        report = plans.get(provider) or {}
        saved = bool(report.get("model") and report.get("ends_at"))
        model_items = models.get(provider) or []
        model = str(report.get("model") or "")
        speed = str(report.get("speed") or "standard").lower()
        speeds = next((m.get("speeds") or ["standard"] for m in model_items if m.get("id") == model), ["standard"])
        if "standard" not in [str(s).lower() for s in speeds]:
            speeds = ["standard", *speeds]
        disabled = " disabled" if not live else ""
        parts.extend([
            f'<form class="credit-plan" data-credit-provider="{provider}" aria-label="{title} paid-credit plan">',
            f'<h3>{title} <small>paid {unit}</small></h3>',
            f'<label>Amount ({unit})<input name="amount" type="number" min="0.000001" step="any" inputmode="decimal" required value="{_esc(report.get("amount") or "")}"{disabled}></label>',
            f'<label>Start <input name="starts_at" type="datetime-local" required value="{_esc(_local_input(report.get("starts_at")) if saved else start_default)}"{disabled}></label>',
            f'<label>Deadline <input name="ends_at" type="datetime-local" required value="{_esc(_local_input(report.get("ends_at")))}"{disabled}></label>',
            f'<label>Forecast model <select name="model" required{disabled}><option value="">Choose a model</option>',
        ])
        for item in model_items:
            key = str(item.get("id") or "")
            if key:
                parts.append(f'<option value="{_esc(key)}"{" selected" if key == model else ""}>{_esc(item.get("label") or key)}</option>')
        parts.append('</select></label><label>Speed mode <select name="speed" required' + disabled + '>')
        for value in speeds:
            key = str(value).lower()
            label = str(value).replace("_", " ").title()
            parts.append(f'<option value="{_esc(key)}"{" selected" if key == speed else ""}>{_esc(label)}</option>')
        parts.extend([
            '</select></label><div class="credit-actions">',
            f'<button type="submit"{disabled}>Save</button>',
            f'<button type="button" class="credit-clear"{disabled}>Clear</button></div>',
            '<p class="credit-form-message" role="status" aria-live="polite"></p>',
            '<div class="credit-plan-result" aria-live="polite"></div>',
            '</form>',
        ])
    parts.append('</div></section>')
    return "".join(parts)


CSS = """
.credit-plans{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px;margin:8px 0 18px;border-top:4px solid #f59e0b}
.credit-plans h2{font-size:15px;margin:0;color:var(--muted);text-transform:uppercase;letter-spacing:.06em}
.credit-plans>.note{margin:4px 0 0}
.credit-plan-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:16px;margin-top:12px}
.credit-plan{min-width:0;padding:12px 14px;border:1px solid var(--line);border-radius:8px;background:var(--bg)}
.credit-plan h3{margin:0 0 8px;font-size:14px}.credit-plan h3 small{font-size:12px;font-weight:400;color:var(--muted)}
.credit-plan label{display:grid;gap:3px;margin:8px 0;font-size:12px;color:var(--muted)}
.credit-plan input,.credit-plan select{width:100%;font:inherit;font-size:13px;padding:4px 8px;border-radius:6px;border:1px solid var(--line);background:var(--card);color:var(--fg);color-scheme:light dark;outline:none}
.credit-plan input:focus,.credit-plan select:focus{border-color:#f59e0b;box-shadow:0 0 0 1px #f59e0b}
.credit-plan input:disabled,.credit-plan select:disabled{opacity:.6}
.credit-actions{display:flex;gap:6px;margin:12px 0 4px}
.credit-actions button{font:inherit;font-size:12px;padding:3px 12px;border:1px solid var(--line);background:var(--card);color:var(--fg);border-radius:999px;cursor:pointer}
.credit-actions button[type=submit]{border-color:#f59e0b;color:#d97706;font-weight:600}
.credit-actions button:hover:not(:disabled){border-color:#f59e0b}
.credit-actions button:focus-visible{outline:2px solid #f59e0b;outline-offset:1px}
.credit-actions button:disabled{opacity:.5;cursor:default}
.credit-form-message{min-height:1.2em;margin:4px 0;font-size:12px;color:var(--warn)}
.credit-plan-result{line-height:1.5;font-size:12px;color:var(--muted)}
.credit-plan-result p{margin:4px 0}
.credit-plan-result svg{display:block;width:100%;max-width:460px;height:140px;background:var(--card);border:1px solid var(--line);border-radius:6px;margin:8px 0}
.credit-forecast-overlay{margin:10px 0 0;padding:8px 10px;border-left:3px solid #f59e0b;background:var(--grid);border-radius:0 6px 6px 0;font-size:12px;color:var(--fg)}
.credit-forecast-overlay strong{display:block;color:#d97706}.credit-forecast-overlay p{margin:3px 0;color:var(--muted)}
.credit-forecast-overlay svg{display:block;width:100%;max-width:460px;height:auto;margin:8px 0;background:var(--card);border:1px solid var(--line);border-radius:6px}
.cg-goal{stroke:var(--chart-axis);stroke-width:2;stroke-dasharray:6 4}
.cg-pace{stroke:#f59e0b;stroke-width:2;stroke-dasharray:5 3}
.cg-pace-high{stroke:#fbbf24;stroke-width:2;stroke-dasharray:5 3}
.cg-spent{fill:var(--under)}
.cg-band{fill:#f59e0b;fill-opacity:.2}
.cg-axis{stroke:var(--chart-axis)}
.cg-lbl{fill:var(--chart-muted);font-size:11px;font-variant-numeric:tabular-nums}
@media (prefers-color-scheme: dark){
  .credit-actions button[type=submit],.credit-forecast-overlay strong{color:#fbbf24}
}
"""


SCRIPT = r"""
(function(){
  'use strict';
  var root=document.getElementById('credit-plans'); if(!root) return;
  var initial=__INITIAL__;
  var state=initial, inflight=false, revision=Number(initial.revision || 0);
  var names={codex:'Codex',claude:'Claude'}, units={codex:'credits',claude:'USD'};
  var forms=Array.prototype.slice.call(root.querySelectorAll('form[data-credit-provider]'));
  function elem(tag,text,cls){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=String(text);return e;}
  function clear(el){while(el.firstChild)el.removeChild(el.firstChild);}
  function num(v){if(v===null||v===undefined||v==='')return null;var n=Number(v);return Number.isFinite(n)?n:null;}
  function fmt(v,digits){var n=num(v);return n===null?'unavailable':n.toLocaleString(undefined,{maximumFractionDigits:digits===undefined?3:digits});}
  function utcInput(v){if(!v)return '';var d=new Date(v);if(isNaN(d.getTime()))return '';var z=function(n){return String(n).padStart(2,'0');};return d.getFullYear()+'-'+z(d.getMonth()+1)+'-'+z(d.getDate())+'T'+z(d.getHours())+':'+z(d.getMinutes());}
  function iso(v){var d=new Date(v);return isNaN(d.getTime())?'':d.toISOString();}
  function active(report){if(!report || !report.model || !report.ends_at||report.status==='complete')return false;var n=Date.now(), s=Date.parse(report.starts_at||''), e=Date.parse(report.ends_at);return Number.isFinite(s)&&Number.isFinite(e)&&s<=n&&n<e;}
  function activeMap(data){var p=data.plans||{}, out={};Object.keys(names).forEach(function(k){out[k]=active(p[k]);});return out;}
  function textLine(parent,text){parent.appendChild(elem('p',text));}
  function drawGraph(parent,report,provider){
    var start=Date.parse(report.starts_at), end=Date.parse(report.ends_at), now=Date.now();
    if(!Number.isFinite(start)||!Number.isFinite(end)||end<=start)return;
    var target=num(report.amount), spent=num(report.confirmed_spent);
    if(target===null||target<=0)return;
    var x=Math.max(0,Math.min(1,(now-start)/(end-start)));
    var y=spent===null?null:Math.max(0,Math.min(1,spent/target));
    var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('viewBox','0 0 420 140');svg.setAttribute('role','img');
    svg.setAttribute('aria-label',names[provider]+' paid '+units[provider]+' goal: original straight-line goal, current required pace'+(y===null?'; confirmed spending unavailable':'; confirmed '+(report.progress_known?'spending ':'spending lower bound ')+fmt(spent,4)+' of '+fmt(target,4)));
    function line(x1,y1,x2,y2,cls){var l=document.createElementNS('http://www.w3.org/2000/svg','line');[['x1',x1],['y1',y1],['x2',x2],['y2',y2],['class',cls]].forEach(function(a){l.setAttribute(a[0],a[1]);});svg.appendChild(l);}
    line(24,116,396,16,'cg-goal');
    if(y!==null){
      var point=document.createElementNS('http://www.w3.org/2000/svg','circle');
      point.setAttribute('cx',24+372*x);point.setAttribute('cy',116-100*y);point.setAttribute('r',5);
      point.setAttribute('class','cg-spent');svg.appendChild(point);
      line(24+372*x,116-100*y,396,16,'cg-pace');
    }
    else {line(24+372*x,116,396,16,'cg-pace');}
    parent.appendChild(svg);
    textLine(parent,'Original goal · · ·   Required future pace · · ·   '+(y===null?'Actual paid spending unavailable':report.progress_known?'Confirmed spend ●':'Confirmed spending lower bound ●'));
  }
  function showReport(form,report){
    var box=form.querySelector('.credit-plan-result');clear(box);
    var provider=form.dataset.creditProvider, unit=units[provider], observation=(state.observations||{})[provider]||{};
    if(observation.balance!==null && observation.balance!==undefined)textLine(box,'Provider-reported balance: '+fmt(observation.balance,6)+' '+unit+' · '+String(observation.freshness||'unknown freshness')+'. Balance is not a spending counter.');
    if(observation.note)textLine(box,observation.note);
    if(!report||!report.model){textLine(box,'No paid-credit plan saved.');return;}
    var forecast=report.forecast||{};
    textLine(box,'Goal: '+fmt(report.amount,6)+' '+unit+' by '+new Date(report.ends_at).toLocaleString()+'. '+String(report.status||'Saved')+'.');
    if(report.progress_known && report.confirmed_spent!==null && report.confirmed_spent!==undefined){
      textLine(box,'Confirmed paid spend: '+fmt(report.confirmed_spent,6)+' '+unit+'; remaining: '+fmt(report.remaining,6)+' '+unit+'.');
    }else if(num(report.confirmed_spent)!==null){textLine(box,'Spending history has gaps. Confirmed paid spend is at least '+fmt(report.confirmed_spent,6)+' '+unit+'; the conservative remaining budget is '+fmt(report.remaining,6)+' '+unit+'.');}
    else{textLine(box,'Forecast only: paid spending is unavailable; the full unverified budget remains in the pace calculation.');}
    textLine(box,'Required paid-spend pace: '+fmt(report.required_rate_per_hour,6)+' '+unit+'/hour. Original goal: '+fmt(report.original_rate_per_hour,6)+' '+unit+'/hour.');
    if(report.note)textLine(box,report.note);
    if(forecast.available){
      textLine(box,'Estimated token capacity: '+fmt(forecast.token_capacity_low,0)+'–'+fmt(forecast.token_capacity_high,0)+' tokens; required pace '+fmt(forecast.required_tokens_per_hour_low,0)+'–'+fmt(forecast.required_tokens_per_hour_high,0)+' tokens/hour.');
    }else{textLine(box,'Token capacity estimate unavailable'+(forecast.note?': '+forecast.note:'.'));}
    if(provider==='claude' && (forecast.hypothetical||((state.observations||{}).claude||{}).enabled===false || String(report.note||'').toLowerCase().indexOf('disabled')>=0))textLine(box,'Hypothetical: Claude paid usage is disabled.');
    if(provider==='codex' && observation.enabled===false)textLine(box,'Hypothetical: provider reports no available paid credits.');
    drawGraph(box,report,provider);
  }
  function populate(form,data){
    var provider=form.dataset.creditProvider, report=(data.plans||{})[provider]||{}, registry=(data.models||{})[provider]||[];
    var select=form.elements.model, speed=form.elements.speed, previousModel=select.value, previousSpeed=speed.value;
    var dirty=form.dataset.dirty==='true';
    if(!dirty){form.elements.amount.value=report.amount||'';form.elements.starts_at.value=utcInput(report.starts_at)||utcInput(new Date().toISOString());form.elements.ends_at.value=utcInput(report.ends_at);}
    clear(select);select.appendChild(elem('option','Choose a model'));select.options[0].value='';
    registry.forEach(function(item){var option=elem('option',item.label||item.id);option.value=item.id;select.appendChild(option);});
    select.value=dirty?previousModel:(report.model||'');
    if(select.selectedIndex<0)select.value='';
    var selected=registry.find(function(item){return item.id===select.value;});
    var speeds=(selected&&selected.speeds)||['standard'];if(!speeds.some(function(s){return String(s).toLowerCase()==='standard';}))speeds=['standard'].concat(speeds);
    clear(speed);speeds.forEach(function(s){var option=elem('option',String(s).replace(/_/g,' '));option.value=String(s).toLowerCase();speed.appendChild(option);});
    speed.value=dirty?previousSpeed:String(report.speed||'standard').toLowerCase();if(speed.selectedIndex<0)speed.value='standard';
    showReport(form,report);
  }
  function forecastTrajectory(parent,report,w,forecast,provider){
    var calibration=num(w.calibration_tokens_per_point), lowRate=num(forecast.required_tokens_per_hour_low), highRate=num(forecast.required_tokens_per_hour_high);
    var end=Date.parse(report.ends_at), now=Date.now(), hours=(end-now)/3600000;
    if(calibration===null||calibration<=0||lowRate===null||highRate===null||hours<=0)return false;
    var low=Math.max(0,Math.min(lowRate,highRate)*hours/calibration);
    var high=Math.max(0,Math.max(lowRate,highRate)*hours/calibration);
    if(!Number.isFinite(low)||!Number.isFinite(high))return false;
    var max=Math.max(high*1.12,1), bottom=124, top=18, left=43, right=392;
    var y=function(value){return bottom-(bottom-top)*value/max;};
    var svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
    svg.setAttribute('class','credit-workload-trajectory');svg.setAttribute('viewBox','0 0 420 168');
    svg.setAttribute('role','img');svg.setAttribute('aria-label',names[provider]+' projected cumulative workload from now to deadline: '+fmt(low,1)+' to '+fmt(high,1)+' quota-equivalent points; forecast only, not measured quota');
    function shape(tag,attrs){var e=document.createElementNS('http://www.w3.org/2000/svg',tag);Object.keys(attrs).forEach(function(k){e.setAttribute(k,attrs[k]);});svg.appendChild(e);return e;}
    function label(x,ypos,value,anchor){var e=shape('text',{x:x,y:ypos,'class':'cg-lbl','text-anchor':anchor||'start'});e.textContent=value;return e;}
    shape('line',{x1:left,y1:bottom,x2:right,y2:bottom,'class':'cg-axis'});
    shape('line',{x1:left,y1:bottom,x2:left,y2:top,'class':'cg-axis'});
    shape('polygon',{points:left+','+bottom+' '+right+','+y(high)+' '+right+','+y(low),'class':'cg-band'});
    shape('line',{x1:left,y1:bottom,x2:right,y2:y(low),'class':'cg-pace'});
    shape('line',{x1:left,y1:bottom,x2:right,y2:y(high),'class':'cg-pace-high'});
    label(left-6,bottom+3,'0','end');label(left-6,top+4,fmt(max,0),'end');
    label(left,bottom+19,new Date(now).toLocaleString(), 'start');
    label(right,bottom+19,new Date(end).toLocaleString(), 'end');
    label(right,top+3,fmt(low,1)+'–'+fmt(high,1)+' pp','end');
    parent.appendChild(svg);
    textLine(parent,'Cumulative forecast workload · quota-equivalent points. Required slope '+fmt(Math.min(lowRate,highRate)/calibration,2)+'–'+fmt(Math.max(lowRate,highRate)/calibration,2)+' points/hour; '+fmt(low,1)+'–'+fmt(high,1)+' points by deadline. Dashed gold range is projected workload, not observed quota.');
    return true;
  }
  function overlay(data){
    Array.prototype.forEach.call(document.querySelectorAll('.credit-forecast-overlay'),function(el){el.remove();});
    var plans=data.plans||{};
    Array.prototype.forEach.call(document.querySelectorAll('article.card[data-provider][data-window]'),function(card){
      var provider=card.dataset.provider, report=plans[provider];if(!active(report))return;
      var windowKey=card.dataset.window, forecast=report.forecast||{}, windows=forecast.windows||[];
      var capacityKey=windowKey.replace(/^5h(?=:|$)/,'300m').replace(/^7d(?=:|$)/,'10080m');
      var w=windows.find(function(item){return item.key===capacityKey || item.key===capacityKey+':'+provider || item.key===windowKey || item.key===provider+':'+windowKey || item.window===windowKey;});
      var panel=elem('div',undefined,'credit-forecast-overlay');
      panel.setAttribute('aria-label','Paid-credit forecast for '+names[provider]+' '+windowKey);
      panel.appendChild(elem('strong','Paid-credit forecast · '+windowKey));
      if(w && forecast.available){
        textLine(panel,'Additional estimated quota equivalent: '+fmt(w.extra_percentage_points_low,1)+'–'+fmt(w.extra_percentage_points_high,1)+' percentage points. Measured quota above remains 0–100%.');
        textLine(panel,'Included capacity remaining: '+fmt(w.remaining_included_tokens,0)+' tokens. Projected replenishment at resets: '+fmt(w.projected_replenishment_tokens,0)+' tokens.');
        if(w.transition_at)textLine(panel,'Expected transition to paid usage: '+new Date(w.transition_at).toLocaleString()+'.');
        if(!forecastTrajectory(panel,report,w,forecast,provider))textLine(panel,'Workload trajectory unavailable: token pace or quota calibration is missing.');
      }else{textLine(panel,'Paid-capacity estimate unavailable for this window'+(forecast.note?': '+forecast.note:'.'));}
      if((report.forecast||{}).hypothetical||((data.observations||{})[provider]||{}).enabled===false)textLine(panel,'Hypothetical while paid usage is disabled.');
      card.appendChild(panel);
    });
  }
  function apply(data){
    if(!data||typeof data!=='object'||Number(data.revision||0)<revision)return;
    var before=JSON.stringify(window.qbCreditPlans||{});state=data;revision=Number(data.revision||0);window.qbCreditPlans=activeMap(data);
    forms.forEach(function(form){populate(form,data);});overlay(data);
    if(before!==JSON.stringify(window.qbCreditPlans))document.dispatchEvent(new Event('credit-plans:updated'));
  }
  function refresh(){
    if(inflight)return Promise.resolve();inflight=true;
    return fetch('/v1/plans',{headers:{Accept:'application/json'},cache:'no-store'}).then(function(response){if(!response.ok)throw Error('Plan refresh failed ('+response.status+').');return response.json();}).then(apply).catch(function(error){forms.forEach(function(form){form.querySelector('.credit-form-message').textContent=error.message;});}).finally(function(){inflight=false;});
  }
  function post(form,plan){
    var provider=form.dataset.creditProvider, button=form.querySelector('button[type=submit]');button.disabled=true;
    form.querySelector('.credit-form-message').textContent='Saving…';
    fetch('/v1/plans',{method:'POST',headers:{'Content-Type':'application/json',Accept:'application/json'},body:JSON.stringify({expected_revision:revision,provider:provider,plan:plan})})
      .then(function(response){return response.json().catch(function(){return {};}).then(function(body){if(!response.ok){var error=Error(body.error||body.message||('Save failed ('+response.status+').'));error.conflict=response.status===409;throw error;}return {body:body,pending:response.status===202};});})
      .then(function(result){if(result.pending){form.querySelector('.credit-form-message').textContent='Save queued; waiting for durable confirmation.';refresh();return;}form.dataset.dirty='false';delete form.dataset.startEdited;form.querySelector('.credit-form-message').textContent=plan?'Saved.':'Cleared.';apply(result.body);})
      .catch(function(error){form.querySelector('.credit-form-message').textContent=error.message+(error.conflict?' Current plans will reload; your edits are kept.':'');if(error.conflict)refresh();})
      .finally(function(){button.disabled=false;});
  }
  forms.forEach(function(form){
    form.addEventListener('input',function(event){form.dataset.dirty='true';if(event.target===form.elements.starts_at)form.dataset.startEdited='true';});
    form.addEventListener('change',function(event){form.dataset.dirty='true';if(event.target===form.elements.starts_at)form.dataset.startEdited='true';if(event.target===form.elements.model){var item=((state.models||{})[form.dataset.creditProvider]||[]).find(function(m){return m.id===form.elements.model.value;});var speed=form.elements.speed;clear(speed);var modes=(item&&item.speeds)||['standard'];modes.forEach(function(s){var o=elem('option',String(s).replace(/_/g,' '));o.value=String(s).toLowerCase();speed.appendChild(o);});speed.value='standard';}});
    form.addEventListener('submit',function(event){event.preventDefault();var amount=form.elements.amount.value.trim(),start=iso(form.elements.starts_at.value),end=iso(form.elements.ends_at.value),model=form.elements.model.value,speed=form.elements.speed.value;
      var savedPlan=(state.plans||{})[form.dataset.creditProvider];
      if(form.dataset.startEdited!=='true')start=savedPlan?savedPlan.starts_at:new Date().toISOString();
      if(!form.reportValidity())return;
      if(!start||!end||Date.parse(end)<=Date.parse(start)){form.querySelector('.credit-form-message').textContent='Deadline must be after start.';return;}
      if(!/^(?:\d+)(?:\.\d+)?$/.test(amount)||Number(amount)<=0){form.querySelector('.credit-form-message').textContent='Enter a positive credit amount.';return;}
      post(form,{amount:amount,starts_at:start,ends_at:end,model:model,speed:speed});
    });
    form.querySelector('.credit-clear').addEventListener('click',function(){post(form,null);});
  });
  apply(initial);refresh();setInterval(refresh,30000);
  document.addEventListener('dashboard:updated',function(){overlay(state);refresh();});
})();
"""


def script(snapshot: dict[str, Any] | None) -> str:
    return SCRIPT.replace("__INITIAL__", _script_json(snapshot or {"revision": 0, "plans": {}, "models": {}}))
