// Bounded, best-effort human-operation measurements. Never reads input values or bodies.
export const telemetryActions=new Set(['view_open','store_period_open','manual_amount_edit','month_advance','business_decision_preview','business_decision_apply','business_decision_revoke','month_close_preview','month_close_confirm','manual_cost_preview','period_close','cost_line_save','cost_batch_preview','cost_batch_apply','labor_total_save','payout_confirm','relationship_confirm','waiting_summary_open','waiting_deliver_open'])
const kinds=new Set(['page_open','store_period_open','manual_amount_edit','manual_confirm','operation_complete'])
const views=new Set(['board','period','deliver','commission','fees','labor','freight'])
const outcomes=new Set(['success','failure','observed'])
let queue=[],timer,authenticated=false,clientId='',context=()=>({}),originalFetch,installed=false,lastAuth=0
export function clientRequestId(){
  if(globalThis.crypto?.randomUUID)return globalThis.crypto.randomUUID()
  const bytes=new Uint8Array(16)
  if(globalThis.crypto?.getRandomValues)globalThis.crypto.getRandomValues(bytes)
  else for(let index=0;index<16;index++)bytes[index]=Math.floor(Math.random()*256)
  bytes[6]=(bytes[6]&15)|64;bytes[8]=(bytes[8]&63)|128
  const hex=[...bytes].map(value=>value.toString(16).padStart(2,'0')).join('')
  return `${hex.slice(0,8)}-${hex.slice(8,12)}-${hex.slice(12,16)}-${hex.slice(16,20)}-${hex.slice(20)}`
}
const id=clientRequestId
export function sanitizeTelemetry(event){
  if(!kinds.has(event.kind)||!telemetryActions.has(event.action)||!outcomes.has(event.outcome||'observed'))return null
  const view=String(event.view||'board')
  return {event_id:event.event_id||id(),kind:event.kind,at:event.at||new Date().toISOString(),view:views.has(view)?view:'board',
    store_id:/^[A-Za-z0-9_-]{1,100}$/.test(event.store_id||'')?event.store_id:'',period:/^\d{4}-(0[1-9]|1[0-2])$/.test(event.period||'')?event.period:'',
    action:event.action,duration_ms:Math.max(0,Math.min(86400000,Math.round(Number(event.duration_ms)||0))),outcome:event.outcome||'observed'}
}
export function apiTelemetryAction(url,method='GET'){
  if(method.toUpperCase()!=='POST')return null
  const path=String(url).split('?')[0]
  if(/^\/api\/automation\/decisions\/[^/]+\/revoke$/.test(path))return ['manual_confirm','business_decision_revoke']
  if(/^\/api\/automation\/months\/\d{4}-\d{2}\/advance$/.test(path))return ['operation_complete','month_advance']
  const fixed={'/api/automation/decisions/preview':['operation_complete','business_decision_preview'],'/api/automation/decisions/apply':['manual_confirm','business_decision_apply'],
    '/api/automation/close-batches/preview':['operation_complete','month_close_preview'],'/api/automation/close-batches/confirm':['manual_confirm','month_close_confirm'],
    '/api/commission-v2/labor':['manual_confirm','labor_total_save'],'/api/commission-v2/payouts/confirm':['manual_confirm','payout_confirm']}
  if(fixed[path])return fixed[path]
  if(/^\/api\/stores\/[^/]+\/periods\/\d{4}-\d{2}\/close$/.test(path))return ['manual_confirm','period_close']
  if(/^\/api\/stores\/[^/]+\/periods\/\d{4}-\d{2}\/manual-cost-preview$/.test(path))return ['operation_complete','manual_cost_preview']
  if(/^\/api\/stores\/[^/]+\/periods\/\d{4}-\d{2}\/cost-lines$/.test(path))return ['manual_confirm','cost_line_save']
  if(/\/cost-lines\/batch-preview$/.test(path))return ['operation_complete','cost_batch_preview']
  if(/\/cost-lines\/batch-apply$/.test(path))return ['manual_confirm','cost_batch_apply']
  if(/^\/api\/commission-v2\/(schemes|batch)\/[^?]*apply$/.test(path))return ['manual_confirm','relationship_confirm']
  return null
}
export function recordTelemetry(event){
  if(!installed)return
  const clean=sanitizeTelemetry({...context(),...event})
  if(!clean)return
  queue.push(clean);if(queue.length>50)queue=queue.slice(-50)
  if(!timer)timer=setTimeout(()=>{timer=null;flushTelemetry()},1500)
}
export function setTelemetryAuthenticated(value){authenticated=!!value;if(authenticated&&queue.length)flushTelemetry()}
export function notifyAuthentication(status,detail){
  if(status!==401 && !(status===403 && detail?.includes('登录')))return
  if(typeof window==='undefined'||!window.dispatchEvent||Date.now()-lastAuth<500)return
  lastAuth=Date.now();authenticated=false
  window.dispatchEvent(new CustomEvent('ledger:auth-required',{detail:detail||'请先登录操作账号'}))
}
export async function flushTelemetry(){
  if(!authenticated||!queue.length||!originalFetch)return
  const events=queue.splice(0,50)
  try{await originalFetch('/api/automation/telemetry',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({client_id:clientId,events}),keepalive:true})}catch{/* Measurement never blocks or retries a business action. */}
}
export function startTelemetry(getContext){
  if(installed||typeof window==='undefined')return ()=>{}
  installed=true;context=getContext;originalFetch=window.fetch.bind(window)
  try{clientId=sessionStorage.getItem('ledger-telemetry-session')||id();sessionStorage.setItem('ledger-telemetry-session',clientId)}catch{clientId=id()}
  window.fetch=async(url,init={})=>{
    const action=apiTelemetryAction(typeof url==='string'?url:'',init.method||'GET'),started=performance.now()
    try{
      const response=await originalFetch(url,init)
      if(action)recordTelemetry({kind:action[0],action:action[1],duration_ms:performance.now()-started,outcome:response.ok?'success':'failure'})
      if(response.status===401||response.status===403){const body=await response.clone().json().catch(()=>({}));notifyAuthentication(response.status,typeof body.detail==='string'?body.detail:'')}
      return response
    }catch(error){if(action)recordTelemetry({kind:action[0],action:action[1],duration_ms:performance.now()-started,outcome:'failure'});throw error}
  }
  const focused=new WeakMap()
  const monetary=target=>target instanceof HTMLInputElement&&(target.inputMode==='decimal'||target.type==='number'||!!target.closest('[inputmode="decimal"]'))
  const focus=event=>{if(monetary(event.target))focused.set(event.target,{at:performance.now(),dirty:false})}
  const input=event=>{if(monetary(event.target)){const entry=focused.get(event.target)||{at:performance.now(),dirty:false};entry.dirty=true;focused.set(event.target,entry)}}
  const blur=event=>{const entry=focused.get(event.target);if(entry?.dirty){recordTelemetry({kind:'manual_amount_edit',action:'manual_amount_edit',duration_ms:performance.now()-entry.at});focused.delete(event.target)}}
  document.addEventListener('focusin',focus,true);document.addEventListener('input',input,true);document.addEventListener('focusout',blur,true);window.addEventListener('pagehide',flushTelemetry)
  recordTelemetry({kind:'page_open',action:'view_open'})
  return ()=>{clearTimeout(timer);flushTelemetry();window.fetch=originalFetch;installed=false;document.removeEventListener('focusin',focus,true);document.removeEventListener('input',input,true);document.removeEventListener('focusout',blur,true);window.removeEventListener('pagehide',flushTelemetry)}
}
