// Fixed local source capability. There is no arbitrary URL, field, SQL or command.
import fs from 'node:fs';
import path from 'node:path';
import {fileURLToPath,pathToFileURL} from 'node:url';
import crypto from 'node:crypto';
import {MemberEvidenceReader} from './jst_member_reader.mjs';

const sha=raw=>crypto.createHash('sha256').update(raw).digest('hex');
const schema='jst_source_recovery.v1';
const [requestFile]=process.argv.slice(2);
const request=JSON.parse(fs.readFileSync(requestFile,'utf8'));
const {plan,task_id,job_id,plan_sha256,folder,jst_module,jst_sha256}=request;
const dateLookup=plan.purpose==='original_day';
if(plan.purpose&&!['source_members','original_day'].includes(plan.purpose))throw Error('invalid_fixed_source_purpose');
const own=fileURLToPath(import.meta.url),ownSHA=sha(fs.readFileSync(own));
const readerPath=path.join(path.dirname(own),'jst_member_reader.mjs');
const readerSHA=sha(fs.readFileSync(readerPath));
if(readerSHA!=='0c636672b044492c249572b7f3940eaa4cdf9c3f0d05b8761ac7ddd3929477b8')throw Error('fixed_reader_identity_changed');
if(plan.schema!==schema||!/^source-[0-9a-f]{40}$/.test(task_id)||! /^[0-9a-f]{32}$/.test(job_id)||!path.isAbsolute(folder)||!path.isAbsolute(jst_module))throw Error('invalid_fixed_source_request');
fs.mkdirSync(folder,{recursive:true,mode:0o700});
function write(name,raw){
 const target=path.join(folder,name);
 if(fs.existsSync(target)){
  if(sha(fs.readFileSync(target))!==sha(raw))throw Error('immutable_source_artifact_changed');
  return;
 }
 const temp=target+'.'+crypto.randomUUID()+'.tmp';
 const fd=fs.openSync(temp,'wx',0o600);
 try{fs.writeFileSync(fd,raw);fs.fsyncSync(fd)}finally{fs.closeSync(fd)}
 fs.renameSync(temp,target);
 if(process.platform!=='win32'){const d=fs.openSync(folder,'r');try{fs.fsyncSync(d)}finally{fs.closeSync(d)}}
}
const receipt={schema,task_id,job_id,plan_sha256,input_sha:plan.input_sha,
 parser_sha256:readerSHA,collector_sha256:ownSHA,jst_sha256,
 state:'pending',reason:null,nodes:[],relations:[],responses:[],
 financial_applied:false,business_complete:false,started_at:new Date().toISOString()};
let stopped=false,last=0,bytes=0,network=0;
const deadline=Date.now()+Math.min(120,plan.limits.seconds)*1000;
const fail=code=>{const error=new Error(code);error.code=code;throw error};
const nativeFetch=globalThis.fetch;
const allowed=new Set(['/app/order/order/list.aspx','/app/order/order/OrderEditorNew.aspx','/app/order/order/MergeOrder.aspx']);
globalThis.fetch=async(input,init={})=>{
 if(stopped||Date.now()>=deadline)fail('source_budget_or_stop');
 const url=new URL(String(input));
 if(url.protocol!=='https:'||url.hostname!=='www.erp321.com'||!allowed.has(url.pathname))fail('source_route_denied');
 const method=init.method||'GET';
 if(method!=='GET'){
  if(method!=='POST'||url.pathname!=='/app/order/order/list.aspx'||url.searchParams.get('am___')!=='LoadDataToJSON')fail('source_method_denied');
  const callback=JSON.parse(new URLSearchParams(init.body).get('__CALLBACKPARAM')||'null');
  if(callback?.Method!=='LoadDataToJSON')fail('source_callback_denied');
 }
 const delay=Math.max(1000,plan.limits.request_interval_ms)-(Date.now()-last);
 if(delay>0)await new Promise(resolve=>setTimeout(resolve,delay));
 if(Date.now()>=deadline)fail('source_time_budget');
 last=Date.now();network++;
 let response,raw;
 try{
  response=await nativeFetch(input,{...init,redirect:'error'});
  const stream=response.clone().body.getReader(),chunks=[];
  let length=0;
  while(true){const{value,done}=await stream.read();if(done)break;
   length+=value.length;
   if(length>16*1024*1024||bytes+length>Math.min(plan.limits.body_bytes,32*1024*1024)){await stream.cancel();stopped=true;fail('source_byte_budget')}
   chunks.push(Buffer.from(value));
  }
  raw=Buffer.concat(chunks);
 }catch(error){stopped=true;fail(error.code||'source_transport_unavailable')}
 bytes+=raw.length;const digest=sha(raw);write(digest+'.raw',raw);
 receipt.responses.push({sha256:digest,bytes:raw.length,status:response.status,path:url.pathname,
  method,captured_at:new Date().toISOString()});
 const head=raw.subarray(0,10000).toString('utf8');
 if(response.status!==200||/loginReact|erp-login|无访问权限|未登录|请登录/.test(head)){stopped=true;fail(response.status===405||response.status===429?'source_rate_limited':'source_session_or_http_unavailable')}
 return response;
};
const tokenMatches=(value,shop)=>String(value||'').split(',').map(t=>t.trim()).some(t=>t===plan.platform_order_id||t===String(shop)+':'+plan.platform_order_id);
try{
 if(sha(fs.readFileSync(jst_module))!==jst_sha256)fail('jst_client_identity_changed');
 const {Jst}=await import(pathToFileURL(jst_module).href);
 if(sha(fs.readFileSync(jst_module))!==jst_sha256)fail('jst_client_changed_during_import');
 const jst=new Jst();jst.freshenSession=async()=>fail('source_session_refresh_required');
 const reader=new MemberEvidenceReader({jst,fetchImpl:globalThis.fetch});
 const rows=new Map(),queue=[],nodes=new Map(),relations=new Map();
 if(plan.source_order_id){queue.push({id:plan.source_order_id,depth:0})}
 else{
  const response=await jst.callback('/app/order/order/list.aspx','LoadDataToJSON',
   ['2',JSON.stringify([{k:'so_id',v:plan.platform_order_id,c:'='}]),'{}'],
   {owner_co_id:String(jst.coid),_jt_page_count_enabled:'false',_jt_page_increament_enabled:'true',
    _jt_page_increament_page_mode:'next',_jt_page_increament_key_value:'',_jt_page_increament_business_values:'',
    _jt_page_increament_key_name:'o_id',_jt_page_size:'20',_jt_page_action:'1'});
  if(!Array.isArray(response?.datas)||response.datas.length>20||response.dp?.IsLast===false)fail('platform_source_scope_incomplete');
  const seen=new Set();
  for(const row of response.datas){
   const id=String(row.o_id);
   if((typeof row.o_id==='number'&&!Number.isSafeInteger(row.o_id))||! /^[1-9][0-9]*$/.test(id)||seen.has(id)||!row.o_id_en||!tokenMatches(row.so_id,row.shop_id))fail('platform_source_filter_not_enforced');
   seen.add(id);
   if(!plan.shop_ids.includes(String(row.shop_id))||(!dateLookup&&String(row.order_date).slice(0,10)!==plan.original_day)||row.type!=='普通订单')continue;
   rows.set(id,row);queue.push({id,depth:0});
  }
  if(!queue.length)fail('original_not_found_in_this_scope');
 }
 async function related(id,depth){
  if(relations.has(id))return;
  if(relations.size>=Math.min(6,plan.limits.relations))fail('source_relation_budget');
  const rel=await reader.readRelations(id);relations.set(id,rel);
  receipt.relations.push({order_id:id,sha256:rel.raw_sha256});
  for(const child of rel.links)queue.push({id:child.internal_order_id,depth:depth+1});
 }
 while(true){
  while(queue.length){
   const{id,depth}=queue.shift();if(nodes.has(id))continue;
   if(nodes.size>=Math.min(12,plan.limits.nodes)||depth>Math.min(6,plan.limits.depth))fail('source_graph_budget');
   let row=rows.get(id);
   if(!row){const found=await reader.lookupInternal(id);if(found.state!=='found')fail('explicit_original_not_found_in_this_scope');row=found.rows[0];rows.set(id,row)}
   if(!plan.shop_ids.includes(String(row.shop_id)))fail('source_shop_conflict');
   if(depth===0&&(!tokenMatches(row.so_id,row.shop_id)||(!dateLookup&&String(row.order_date).slice(0,10)!==plan.original_day)))fail('source_original_identity_or_day_changed');
   const detail=await reader.readDetail(row);nodes.set(id,{detail,depth});
   receipt.nodes.push({order_id:id,sha256:detail.raw_sha256});
   const data=detail.data,link=String(data.link_o_id||'');
   if(/^[1-9][0-9]*$/.test(link)&&link!==id)queue.push({id:link,depth:depth+1});
   if(['Split','Merged'].includes(String(data.status)))await related(id,depth);
  }
  let changed=false;
  for(const[id,{detail,depth}]of nodes){const link=String(detail.data.link_o_id||'');
   if(!/^[1-9][0-9]*$/.test(link)||link===id)continue;
   const observed=relations.get(id)?.links.some(r=>r.internal_order_id===link)||relations.get(link)?.links.some(r=>r.internal_order_id===id);
   if(!observed){const parent=String(detail.data.status)==='Merged'?id:link;if(relations.has(parent))fail('typed_source_relation_unproved');await related(parent,depth);changed=true}
  }
  if(!queue.length&&!changed)break;
 }
 receipt.state='raw_ready';
}catch(error){receipt.reason=error.code||'source_tool_unavailable';receipt.error_sha256=sha(String(error.message))}
finally{
 globalThis.fetch=nativeFetch;
 if(sha(fs.readFileSync(own))!==ownSHA||sha(fs.readFileSync(readerPath))!==readerSHA){receipt.state='pending';receipt.reason='source_executor_code_changed'}
 receipt.network_requests=network;receipt.body_bytes=bytes;receipt.ended_at=new Date().toISOString();
 write('receipt.json',Buffer.from(JSON.stringify(receipt)));
 process.stdout.write(JSON.stringify({state:receipt.state,reason:receipt.reason,financial_applied:false,business_complete:false})+'\n');
}
