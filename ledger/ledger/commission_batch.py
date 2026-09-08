"""Preview and atomically apply user-selected commission changes."""
from __future__ import annotations

import hashlib
import io
import json
import re
import uuid
import zipfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from .commission_registry import RegistryError, RevisionConflict, json_text, local_time, now
from .commission_catalog import iter_settings


def people_hash(conn):
    return hashlib.sha256(json_text([tuple(r) for r in conn.execute("SELECT * FROM person ORDER BY id")]).encode()).hexdigest()


def current(conn, store, product, at):
    row = conn.execute("SELECT s.*,v.body FROM scheme s LEFT JOIN scheme_version v ON v.id=s.active_version WHERE store_id=? AND product_id=?", (store,product)).fetchone()
    body = json.loads(row['body'] or '{}') if row else {}
    segment = next((s for s in body.get('segments',[]) if s['valid_from']<=at and (not s.get('valid_to') or at<s['valid_to'])), {})
    return dict(row) if row else {}, segment


def allocations(conn, segment):
    result = {}
    names = {r[0]:r[1] for r in conn.execute('SELECT id,name FROM person')}
    for a in segment.get('allocations',[]):
        pid=a['person_id']; entry=result.setdefault(pid, {'person_id':pid,'name':names.get(pid,pid),'rate':Decimal(0)})
        entry['rate']+=Decimal(a['rate'])
    return [{**r,'rate':str(r['rate'])} for r in result.values()]


def validate_key(model, data, conn):
    if data.get('store_id') not in {s.id for s in model.stores}:
        raise RegistryError('店铺未登记，请从店铺列表选择')
    if not re.fullmatch(r'\d{9,20}|\*', str(data.get('product_id',''))):
        if not conn.execute('SELECT 1 FROM catalog WHERE store_id=? AND product_id=?',(data['store_id'],data.get('product_id',''))).fetchone():
            raise RegistryError('宝贝ID无效，请使用平台宝贝ID并按文本保存')


def preview(registry, model, request, actor):
    source=request.get('source') or {'type':'bulk_settings'}
    entries=request.get('changes')
    if entries is None:
        targets=request.get('targets') or []
        if request.get('scope') is not None:
            scope={k:v for k,v in request['scope'].items() if k in {'store_id','search','state','person_id'}}
            known={s.id for s in model.stores}
            excluded=set(request['scope'].get('excluded',[]))
            targets=[{k:r.get(k) for k in ['store_id','product_id','product_name','revision']} for r in iter_settings(registry,**scope) if r['store_id'] in known and r['store_id']+':'+r['product_id'] not in excluded]
        template=request.get('template') or {}
        entries=[{**template,**t,'expected_revision':t.get('expected_revision',t.get('revision'))} for t in targets]
    if not entries:
        raise RegistryError('请先选择商品或填写新增内容')
    if len(entries)>100000:
        raise RegistryError('单次最多处理10万条设置，请分次导入')
    operation=request.get('operation','replace')
    if operation not in {'replace','merge','remove'}:
        raise RegistryError('请选择批量处理方式')
    changes=[]; display=[]; seen=set()
    names={s.id:s.name for s in model.stores}
    with registry.connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        try:
            fingerprint=people_hash(conn)
            original_people={r[0] for r in conn.execute("SELECT id FROM person")}
            for index,entry in enumerate(entries,1):
                data=dict(entry)
                validate_key(model,data,conn)
                data['valid_from']=local_time(data.get('valid_from',''))
                key=(data['store_id'],data['product_id'],data['valid_from'])
                if key in seen:
                    raise RegistryError(f'第{index}条与前面的店铺、宝贝、生效时间重复')
                seen.add(key)
                old,segment=current(conn,data['store_id'],data['product_id'],data['valid_from'])
                expected=old.get('revision',0)
                if data.get('expected_revision') is not None and data['expected_revision']!=expected:
                    raise RevisionConflict('所选商品已被修改，请刷新后重新预览')
                data['expected_revision']=expected
                catalog=conn.execute('SELECT product_name FROM catalog WHERE store_id=? AND product_id=?',(data['store_id'],data['product_id'])).fetchone()
                data['product_name']=data.get('product_name') or old.get('product_name') or (catalog[0] if catalog else '')
                before=allocations(conn,segment)
                requested=data.get('allocations',[])
                if operation!='replace' and data.get('mode','distribute')=='distribute':
                    by_id={r['person_id']:{'person_id':r['person_id'],'rate':r['rate']} for r in before}
                    extra=[]
                    removed=False
                    for person in requested:
                        pid=person.get('person_id')
                        if not pid and person.get('name'):
                            matches=conn.execute('SELECT id FROM person WHERE name=?',(person['name'],)).fetchall()
                            if len(matches)>1:raise RegistryError('存在同名人员，请选择具体人员')
                            pid=matches[0][0] if matches else None
                        if operation=='remove':
                            if pid and pid in by_id:
                                by_id.pop(pid)
                                removed=True
                        elif pid:by_id[pid]={'person_id':pid,'rate':person.get('rate')}
                        else:extra.append(person)
                    if operation=='remove' and not removed:continue
                    data['allocations']=list(by_id.values())+extra
                    if not data['allocations']:data['mode']='hold'
                data['source']=source
                data['reason']='Excel导入提成设置' if source.get('type')=='settings_excel' else '批量调整提成设置'
                try:
                    saved=registry.save_setting(data,actor,conn=conn)
                except RegistryError as exc:
                    raise type(exc)(f"{names[data['store_id']]} / {data['product_id']}：{exc}") from exc
                after=next(s for s in saved['body']['segments'] if s['valid_from']==data['valid_from'])
                stored=json.loads(json_text(data))
                for person in stored.get('allocations',[]):
                    pid=person.get('person_id')
                    if pid and pid not in original_people:
                        person['name']=conn.execute('SELECT name FROM person WHERE id=?',(pid,)).fetchone()[0]
                        del person['person_id']
                changes.append(stored)
                display.append({'store_id':data['store_id'],'store':names[data['store_id']],'product_id':data['product_id'],
                                'product_name':data['product_name'],'before':before,'after':allocations(conn,after),
                                'mode':after['mode'],'valid_from':after['valid_from'],'valid_to':after['valid_to'],
                                'new':not old,'source_rows':data.get('source_rows',[]),'catalog_missing':not catalog})
        finally:
            conn.rollback()
    if not changes:raise RegistryError('所选商品在指定生效时间没有需要调整的人员')
    bid=str(uuid.uuid4())
    payload={'changes':changes,'people_hash':fingerprint,'rows':display,'source':source}
    with registry.transaction() as conn:
        conn.execute('INSERT INTO setting_batch(id,payload,created_at) VALUES(?,?,?)',(bid,json_text(payload),now()))
    return {'id':bid,'count':len(display),'stores':len({r['store_id'] for r in display}),
            'new_count':sum(r['new'] for r in display),'rows':display,'source':source}


def apply(registry, model, bid, actor):
    with registry.transaction() as conn:
        row=conn.execute('SELECT * FROM setting_batch WHERE id=?',(bid,)).fetchone()
        if not row:raise RegistryError('预览不存在，请重新预览')
        if row['applied_at']:return json.loads(row['result'])
        payload=json.loads(row['payload'])
        if people_hash(conn)!=payload['people_hash']:
            raise RevisionConflict('人员名单已发生变化，请重新预览后保存')
        for data in payload['changes']:
            validate_key(model,data,conn)
            registry.save_setting(data,actor,conn=conn)
        result={'count':len(payload['changes']),'stores':len({x['store_id'] for x in payload['changes']}),'applied':True}
        conn.execute('UPDATE setting_batch SET applied_at=?,result=? WHERE id=?',(now(),json_text(result),bid))
    return result


def parse_excel(raw, filename, model):
    import openpyxl
    if len(raw)>20*1024*1024:raise RegistryError('Excel文件请小于20MB')
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as z:
            if sum(f.file_size for f in z.infolist())>200*1024*1024:raise RegistryError('Excel解压后过大，请拆分文件')
        workbook=openpyxl.load_workbook(io.BytesIO(raw),read_only=True,data_only=False)
    except RegistryError:raise
    except Exception as exc:raise RegistryError('请上传有效的.xlsx文件') from exc
    aliases={}
    for store in model.stores:
        for name in [store.id,store.name,*store.aliases]:aliases.setdefault(name,set()).add(store.id)
    groups={}; errors=[]; effective=0
    headers={'店铺':'store_id','店铺ID':'store_id','店铺（名称或ID）':'store_id','宝贝ID':'product_id','商品名称':'product_name',
             '人员':'person','姓名':'person','所属人员':'person','提成比例':'rate','提成比率':'rate','状态':'mode',
             '生效时间':'valid_from','生效日期':'valid_from','结束时间':'valid_to','失效日期':'valid_to'}
    def text(value):return str(value if value is not None else '').strip().lstrip("'")
    def stamp(value):
        if isinstance(value,(datetime,date)):return value.isoformat()
        return text(value)
    try:
        sheets=[s for s in workbook if s.title not in {'填写说明','示例（不导入）'}]
        for sheet in sheets:
            iterator=sheet.iter_rows(); mapping=None
            for row_no,cells in enumerate(iterator,1):
                if not any(c.value is not None and str(c.value).strip() for c in cells):continue
                if mapping is None:
                    mapping={headers[text(c.value)]:i for i,c in enumerate(cells) if text(c.value) in headers}
                    if not {'store_id','product_id','person','rate','valid_from'}<=mapping.keys():
                        raise RegistryError(f'{sheet.title}缺少必要列，请使用下载的模板')
                    continue
                effective+=1
                if effective>100000:raise RegistryError('单次最多导入10万条有效行')
                where=f'{sheet.title} 第{row_no}行'
                try:
                    def cell(key):return cells[mapping[key]] if key in mapping and mapping[key]<len(cells) else None
                    def value(key):return cell(key).value if cell(key) is not None else None
                    if any(c.data_type=='f' for c in cells):raise ValueError('不接受公式，请粘贴为值后导入')
                    shop=aliases.get(text(value('store_id')),set())
                    if len(shop)!=1:raise ValueError('店铺不存在或名称不唯一，请填写店铺ID')
                    pid=value('product_id')
                    if isinstance(pid,(float,int)):
                        if pid!=int(pid) or len(str(int(pid)))>15:raise ValueError('长宝贝ID必须保存为文本，数字精度不可恢复')
                        pid=str(int(pid))
                    else:pid=text(pid)
                    start=local_time(stamp(value('valid_from')))
                    end=local_time(stamp(value('valid_to')),optional=True)
                    if end and end<=start:raise ValueError('结束时间必须晚于开始时间')
                    mode={'':'distribute','提成中':'distribute','不提成':'exclude','暂不设置':'hold','未设置':'hold'}.get(text(value('mode')))
                    if not mode:raise ValueError('状态请填写提成中、不提成或暂不设置')
                    name=text(value('person')); rawrate=value('rate'); share=Decimal(0)
                    if mode=='distribute':
                        if not name:raise ValueError('请填写人员姓名')
                        if rawrate is None:raise ValueError('请填写提成比例')
                        formatted='%' in cell('rate').number_format
                        if isinstance(rawrate,str) and rawrate.strip().endswith('%'):share=Decimal(rawrate.strip()[:-1])/100
                        elif formatted:share=Decimal(str(rawrate))
                        else:
                            number=Decimal(str(rawrate))
                            if 0<number<1:raise ValueError('小数比例含义不明确，请明确填写例如5%或0.5%')
                            share=number/100
                        if not share.is_finite() or not 0<share<=1:raise ValueError('提成比例必须大于0且不超过100%')
                    key=(next(iter(shop)),pid,start,end)
                    entry=groups.setdefault(key,{'store_id':key[0],'product_id':pid,'product_name':text(value('product_name')),
                        'valid_from':start,'valid_to':end,'mode':mode,'allocations':[],'source_rows':[]})
                    if mode!=entry['mode'] or (text(value('product_name')) and entry['product_name'] and text(value('product_name'))!=entry['product_name']):raise ValueError('同一店铺宝贝的名称或状态冲突')
                    if name and any(a['name']==name for a in entry['allocations']):raise ValueError('同一商品人员重复，请合并点数后保留一行')
                    if mode=='distribute':entry['allocations'].append({'name':name,'rate':str(share)})
                    entry['source_rows'].append(where)
                except (ValueError,RegistryError,InvalidOperation) as exc:errors.append({'row':where,'error':str(exc)})
        if errors: return {'errors':errors,'rows':effective}
        if not groups:raise RegistryError('表格中没有可导入的数据')
        return {'changes':sorted(groups.values(),key=lambda x:(x['store_id'],x['product_id'],x['valid_from'])),
                'rows':effective,'source':{'type':'settings_excel','filename':filename,'sha256':hashlib.sha256(raw).hexdigest()}}
    finally:workbook.close()
