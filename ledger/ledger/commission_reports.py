"""Read stored commission amounts; never recalculate or change a closed period."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date
from decimal import Decimal

from .commission_registry import RegistryError
from .money import money_float


def months(start, end):
    if not re.fullmatch(r"\d{4}-\d{2}", start or '') or not re.fullmatch(r"\d{4}-\d{2}", end or ''):
        raise RegistryError('请选择起止账期')
    try:
        first=date.fromisoformat(start+'-01'); last=date.fromisoformat(end+'-01')
    except ValueError as exc:raise RegistryError('账期格式应为YYYY-MM') from exc
    count=(last.year-first.year)*12+last.month-first.month+1
    if count<1 or count>120:raise RegistryError('结束账期不能早于开始账期，单次最多查询10年')
    return [f'{(first.year*12+first.month-1+i)//12:04d}-{(first.year*12+first.month-1+i)%12+1:02d}' for i in range(count)]


def decimal(value):
    result=Decimal(str(value))
    if not result.is_finite():raise RegistryError('计算结果含无效金额，请核查对应账期')
    return result


def build(workspace, registry, model, start, end, store_ids=None, person_ids=None, run_ids=None):
    periods=months(start,end)
    selected_stores=set(store_ids or []); selected_people=set(person_ids or [])
    names={s.id:s.name for s in model.stores}
    roster={p['id']:p for p in registry.people()}
    where=['r.period>=?','r.period<=?']; args=[start,end]
    if selected_stores:
        where.append('r.store_id IN ('+','.join('?' for _ in selected_stores)+')');args.extend(sorted(selected_stores))
    if run_ids is None:
        source="""FROM period p JOIN run r ON r.id=CASE WHEN p.state='closed' AND p.run_id IS NOT NULL THEN p.run_id
          ELSE (SELECT id FROM run latest WHERE latest.store_id=p.store_id AND latest.period=p.period ORDER BY id DESC LIMIT 1) END"""
    else:
        if len(run_ids)!=len(set(run_ids)):raise RegistryError('计算记录不能重复')
        source='FROM run r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period'
        where.append('r.id IN ('+','.join('?' for _ in run_ids)+')' if run_ids else '0');args.extend(run_ids)
    records=[dict(r) for r in workspace.conn.execute("SELECT r.id,r.store_id,r.period,r.at,json_extract(r.result,'$.commission') commission_json,json_extract(r.result,'$.store') store_name,p.state,p.run_id frozen_id "+source+' WHERE '+' AND '.join(where)+' ORDER BY r.period DESC,r.store_id',args)]
    if run_ids is not None and len(records)!=len(run_ids):raise RegistryError('部分计算记录已不存在或不在所选范围，请重新查询')
    keys=[(r['store_id'],r['period']) for r in records]
    if len(keys)!=len(set(keys)):raise RegistryError('同店同账期只能选择一份计算结果')
    scopes={}; lines=[]; available={}; people_totals={}; store_totals={}; store_people={}
    for record in records:
        c=json.loads(record['commission_json'] or '{}');sid=record['store_id'];period=record['period']
        names.setdefault(sid,record['store_name'] or sid)
        closed=record['state']=='closed' and record['frozen_id']==record['id']
        legacy=c.get('engine')!='commission-v2'
        status='已结账' if closed else ('历史口径' if legacy else ('已计算' if c.get('amount_complete') else '试算'))
        notes=list(c.get('notes') or [])
        if closed and c.get('amount_complete') is False:notes.append('原结账结果保留了试算标记')
        has_result=c.get('total') is not None or any(p.get('amount') is not None for p in c.get('people',[]))
        if not has_result:status='未计算提成'
        all_total=sum((decimal(p['amount']) for p in c.get('people',[]) if p.get('amount') is not None),Decimal(0))
        if c.get('total') is not None and abs(all_total-decimal(c['total']))>Decimal('.01'):
            notes.append('原记录的人员合计与店铺提成合计不一致');status+=' · 合计待核对'
        scope={'store_id':sid,'store':names[sid],'period':period,'finance_run':record['id'],'calculated_at':record['at'],
               'status':status,'has_result':has_result,'notes':'；'.join(notes),'selected_amount':Decimal(0),
               'unassigned_orders':c.get('unassigned_orders'),'base_name':c.get('base_name') or c.get('base_node','')}
        for person in c.get('people',[]):
            if person.get('amount') is None:continue
            name=person.get('person') or '未命名人员'
            pid=person.get('person_id')
            # The v2 legacy bridge also emits name-derived IDs, shared across stores.
            # They do not establish that two same-name payees are the same person.
            if not pid or (pid.startswith('legacy:') and pid not in roster):
                pid='legacy:'+hashlib.sha256((sid+'\0'+name).encode()).hexdigest()
            label=roster.get(pid,{}).get('name') or name
            if pid not in roster:label+=f'（历史记录 · {names[sid]}）'
            available.setdefault(pid,{'id':pid,'name':label})
            if selected_people and pid not in selected_people:continue
            amount=decimal(person['amount']);scope['selected_amount']+=amount
            store_people.setdefault(sid,set()).add(pid)
            lines.append({'person_id':pid,'person':name,'employee_no':roster.get(pid,{}).get('employee_no',''),
                          'store_id':sid,'store':names[sid],'period':period,'amount':money_float(amount),
                          'base':person.get('base'),'base_name':scope['base_name'],'status':status,
                          'calculated_at':record['at'],'finance_run':record['id'],'notes':scope['notes']})
            total=people_totals.setdefault(pid,{'person_id':pid,'person':label,'employee_no':roster.get(pid,{}).get('employee_no',''),
                                               'amount':Decimal(0),'stores':set(),'periods':set(),'statuses':set()})
            total['amount']+=amount;total['stores'].add(sid);total['periods'].add(period);total['statuses'].add(status)
        scopes[(sid,period)]=scope
    invalid=selected_people-set(roster)-set(available)
    if invalid:raise RegistryError('所选人员不存在，请重新选择')
    if selected_stores-set(names):raise RegistryError('所选店铺不存在，请重新选择')
    covered_stores=selected_stores or set(names)
    for sid in sorted(covered_stores):
        total={'store_id':sid,'store':names[sid],'amount':Decimal(0),'people':set(),'periods':0,'missing':0,'statuses':set()}
        for period in periods:
            scope=scopes.get((sid,period))
            if not scope:
                scope={'store_id':sid,'store':names[sid],'period':period,'finance_run':None,'calculated_at':'',
                       'status':'未计算','has_result':False,'notes':'该店铺账期尚无计算结果','selected_amount':None,
                       'unassigned_orders':None,'base_name':''};scopes[(sid,period)]=scope
            if scope['has_result']:
                total['amount']+=scope['selected_amount'];total['periods']+=1
            else:total['missing']+=1
            total['statuses'].add(scope['status'])
        total['people']=store_people.get(sid,set())
        store_totals[sid]=total
    for pid in selected_people-set(people_totals):
        p=roster.get(pid,{})
        people_totals[pid]={'person_id':pid,'person':p.get('name') or available[pid]['name'],'employee_no':p.get('employee_no',''),
                            'amount':None,'stores':set(),'periods':set(),'statuses':{'无对应提成记录'}}
    person_rows=[{**r,'amount':money_float(r['amount']) if r['amount'] is not None else None,'stores':len(r['stores']),
                  'periods':len(r['periods']),'status':'、'.join(sorted(r['statuses']))} for r in people_totals.values()]
    for r in person_rows:r.pop('statuses')
    store_rows=[{**r,'amount':money_float(r['amount']) if r['periods'] else None,'people':len(r['people']),
                 'status':'、'.join(sorted(r['statuses']))} for r in store_totals.values()]
    for r in store_rows:r.pop('statuses')
    coverage=[{**r,'selected_amount':money_float(r['selected_amount']) if r['has_result'] else None} for r in scopes.values()]
    return {'people':sorted(person_rows,key=lambda x:x['person']),'stores':store_rows,'rows':lines,'coverage':coverage,
            'available_people':list(available.values()),'run_ids':[r['id'] for r in records],
            'total':money_float(sum((decimal(x['amount']) for x in lines),Decimal(0))) if any(r['has_result'] for r in coverage) else None,
            'missing_periods':sum(not r['has_result'] for r in coverage),
            'trial_periods':sum('试算' in r['status'] for r in coverage),
            'selection':{'start':start,'end':end,'store_ids':sorted(selected_stores),'person_ids':sorted(selected_people)}}


COLUMNS={
 'people':['人员','工号','提成金额','店铺数','账期数','计算状态','人员ID'],
 'stores':['店铺','提成金额','人员数','已计算账期数','未计算账期数','计算状态'],
 'breakdown':['人员','工号','店铺','账期','提成金额','本人参与基数','基数名称','计算状态','计算时间','说明','计算记录'],
 'coverage':['店铺','账期','筛选范围提成金额','计算状态','未分配订单数','计算时间','说明','计算记录']}


def export_rows(report, kind):
    if kind=='people':
        for r in report['people']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['amount'],r['stores'],r['periods'],r['status'],r['person_id']]))
    elif kind=='stores':
        for r in report['stores']:yield dict(zip(COLUMNS[kind],[r['store'],r['amount'],r['people'],r['periods'],r['missing'],r['status']]))
    elif kind=='breakdown':
        for r in report['rows']:yield dict(zip(COLUMNS[kind],[r['person'],r['employee_no'],r['store'],r['period'],r['amount'],r['base'],r['base_name'],r['status'],r['calculated_at'],r['notes'],r['finance_run']]))
    elif kind=='coverage':
        for r in report['coverage']:yield dict(zip(COLUMNS[kind],[r['store'],r['period'],r['selected_amount'],r['status'],r['unassigned_orders'],r['calculated_at'],r['notes'],r['finance_run']]))
    else:raise RegistryError('请选择导出类型')


def business_export(report, kind):
    columns = {
        'people': [('人员','person'),('工号','employee_no'),('提成金额','amount'),('店铺数','stores'),('月份数','periods'),('状态','status')],
        'stores': [('店铺','store'),('提成金额','amount'),('人数','people'),('已有金额月份','periods'),('未出金额月份','missing'),('状态','status')],
        'breakdown': [('人员','person'),('工号','employee_no'),('店铺','store'),('月份','period'),('提成金额','amount'),('状态','status')],
        'coverage': [('店铺','store'),('月份','period'),('提成金额','selected_amount'),('状态','status'),('未分配人员订单数','unassigned_orders')],
    }[kind]
    def rows():
        for row in report['rows' if kind == 'breakdown' else kind]:
            item = {label:row.get(key) for label,key in columns}
            for original, replacement in [('未计算提成','未出金额'),('未计算','未出金额'),('试算','待核对'),('历史口径','历史提成'),('已计算','待结账'),('无对应提成记录','暂无提成'),('合计待核对','金额待核对')]:
                item['状态'] = item['状态'].replace(original, replacement)
            yield item
    return [label for label,_ in columns], rows()
