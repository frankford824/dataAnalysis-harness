// Read the ERP's original order in both display modes. No business data is written.
// Usage: node capture_order_components.mjs <collector-dir> <output-dir> <order-id> ...
import {createHash} from 'node:crypto';
import {mkdirSync,writeFileSync,renameSync,existsSync,readFileSync} from 'node:fs';
import path from 'node:path';
import {pathToFileURL} from 'node:url';

const [collector,output,...ids]=process.argv.slice(2);
if(!collector||!output||!ids.length||ids.some(id=>!/^\d+$/.test(id))) throw new Error('Provide collector directory, output directory and numeric internal order IDs');
const {Jst}=await import(pathToFileURL(path.join(collector,'jst.mjs')));
const {pool}=await import(pathToFileURL(path.join(collector,'db.mjs')));
const client=await pool.connect();
const plain=new Jst(),expanded=new Jst();
plain.cookie=plain.cookie.split(';').filter(s=>!s.trim().startsWith('combine_show=')).join(';')+'; combine_show=true';
expanded.cookie=expanded.cookie.split(';').filter(s=>!s.trim().startsWith('combine_show=')).join(';')+'; combine_show=false';
const unpack=d=>typeof d==='string'?JSON.parse(d):d;
try {
  await client.query('BEGIN READ ONLY');
  for(const id of ids) {
    const row=(await client.query('SELECT o_id_en FROM jst_order WHERE o_id=$1',[id])).rows[0];
    if(!row) throw new Error(`Order ${id} is absent from the source`);
    const a=unpack(await plain.orderDetail(id,row.o_id_en));
    const b=unpack(await expanded.orderDetail(id,row.o_id_en));
    if(String(a.o_id)!==id||String(b.o_id)!==id||a.shop_id!==b.shop_id||a.modified!==b.modified)
      throw new Error(`Order ${id} changed between reads`);
    const pick=(row,keys)=>Object.fromEntries(keys.map(k=>[k,row[k]]));
    const payload={source:'jst_order_expanded_v1',captured_at:new Date().toISOString(),order_id:id,
      order_store_id:String(a.shop_id),source_modified:a.modified,
      bundles:a.items.filter(x=>x.sku_type==='combine').map(x=>pick(x,['oi_id','sku_id','qty','outer_oi_id'])),
      components:b.items.filter(x=>x.src_combine_sku_id).map(x=>pick(x,['o_id','oi_id','sku_id','qty','src_combine_sku_id','src_combine_sku_qty']))};
    const raw=JSON.stringify(payload),sha=createHash('sha256').update(raw).digest('hex');
    const body=JSON.stringify({sha256:sha,payload_json:raw},null,2);
    mkdirSync(output,{recursive:true});
    const dest=path.join(output,id+'.json');
    if(existsSync(dest)&&readFileSync(dest,'utf8')!==body) {
      const history=path.join(output,'history');mkdirSync(history,{recursive:true});
      const old=readFileSync(dest);writeFileSync(path.join(history,createHash('sha256').update(old).digest('hex')+'.json'),old,{flag:'wx'});
    }
    writeFileSync(dest+'.tmp',body);renameSync(dest+'.tmp',dest);
    console.log(JSON.stringify({order:id,store:payload.order_store_id,bundles:payload.bundles.length,components:payload.components.length,sha256:sha}));
  }
  await client.query('ROLLBACK');
} finally {client.release();await pool.end();}
