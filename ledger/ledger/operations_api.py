"""Read-only management report, drill and export API using one scope contract."""
from __future__ import annotations

import csv
import io

import polars as pl
from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import Response

from . import operations_readmodel as operations


def install(app, workspace, model, registry_factory=None):
    router = APIRouter(prefix='/api/operations', tags=['operations'])

    def sort_rows(rows,key,direction,basis='current'):
        if key not in {*operations.MONEY,*operations.RATES,'name','order_count'} or direction not in {'asc','desc'} or basis not in {'current','mom','yoy'}:
            raise HTTPException(400,'不支持的经营排序方式')
        def order(row):
            value=row.get(key) if key in {'name','order_count'} else operations.decimal(row['metrics'].get(key))
            if basis!='current' and key not in {'name','order_count'}:
                value=operations.decimal(row.get(basis,{}).get(key,{}).get('rate'))
            return (bool(row.get('unclaimed')),value is None, (value if direction=='asc' or isinstance(value,str) else -value) if value is not None else 0, row['id'])
        return sorted(rows,key=order)

    def report(period, scope, compare=True, expected_input_sha='', full_detail=False):
        ws, current_model = workspace(), model()
        try:
            result = operations.read_month(ws,current_model,period,require_detail=full_detail,**scope)
            if expected_input_sha and result['input_sha'] != expected_input_sha:
                raise HTTPException(409,'经营结果已更新，请刷新后重新导出或查看依据')
            if compare:
                previous = operations.read_month(ws,current_model,operations.shifted(period,-1),**scope)
                year = operations.read_month(ws,current_model,operations.shifted(period,-12),**scope)
                for name,baseline in (('mom',previous),('yoy',year)):
                    result.setdefault('comparison_refs',{})[name]={'period':baseline['period'],'input_sha':baseline['input_sha'],'run_refs':baseline['run_refs']}
                    rows = {row['id']:row for row in baseline['rows']}
                    for row in result['rows']:
                        before = rows.get(row['id'],{}).get('metrics',{})
                        row[name] = {key:operations.comparison(operations.decimal(row['metrics'].get(key)),operations.decimal(before.get(key)),rate_metric=key in operations.RATES) for key in operations.DISPLAY}
                    result[name] = {key:operations.comparison(operations.decimal(result['summary'].get(key)),operations.decimal(baseline['summary'].get(key)),rate_metric=key in operations.RATES) for key in operations.DISPLAY}
                result['view_sha']=operations.digest([result['scope_sha'],result['comparison_refs']])
            return result
        except operations.OperationsError as exc:
            raise HTTPException(400,str(exc)) from exc

    @router.get('/month')
    def month(period:str,level:str='store',platform:str='',store_id:str='',team_id:str='',person_id:str='',product_id:str='',managed:str='all',compare:bool=True,
              offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200),sort_key:str='profit_after_labor',sort_direction:str='desc',sort_basis:str='current',managed_split:bool=False):
        if level=='order' and not store_id:
            raise HTTPException(400,'请先选择一家店铺，再查看订单依据')
        result=report(period,dict(level=level,platform=platform,store_id=store_id,team_id=team_id,person_id=person_id,product_id=product_id,managed=managed),compare,full_detail=managed_split)
        ordered=sort_rows(result['rows'],sort_key,sort_direction,sort_basis)
        result.update(total=len(ordered),offset=offset,limit=limit,rows=ordered[offset:offset+limit],reference_total=len(result['reference_rows']) if result['details_loaded'] else None,
                      reference_group_total=len(result['reference_groups']) if result['details_loaded'] else None)
        result['reference_rows']=result['reference_rows'][:200]
        result['reference_groups']=result['reference_groups'][:200]
        return result

    @router.get('/export.csv')
    def export(period:str,level:str='store',platform:str='',store_id:str='',team_id:str='',person_id:str='',product_id:str='',managed:str='all',split:str='',expected_input_sha:str='',sort_key:str='profit_after_labor',sort_direction:str='desc',comparison:str='current',sort_basis:str='current'):
        if comparison not in {'current','mom','yoy'}:raise HTTPException(400,'不支持的比较方式')
        result = report(period,dict(level=level,platform=platform,store_id=store_id,team_id=team_id,person_id=person_id,product_id=product_id,managed=managed,split=split),comparison!='current',expected_input_sha,True)
        out = io.StringIO(newline='')
        writer = csv.writer(out)
        comparison_headers=[] if comparison=='current' else [label for key in operations.DISPLAY for label in (operations.LABELS[key]+'变化额',operations.LABELS[key]+'变化比例',operations.LABELS[key]+'比较说明')]
        managed_headers=['托管：'+operations.LABELS[key] for key in operations.DISPLAY]
        writer.writerow(['经营口径版本','账期','输入版本','范围版本','记录类型','维度','名称','身份','团队','平台','店铺','店铺ID','人员ID','宝贝ID','排名','订单数',*[operations.LABELS[key] for key in operations.DISPLAY],'已保存提成','核算运行ID','比较月份','比较输入版本',*comparison_headers,*managed_headers,'金额精度说明','全范围两位显示尾差（不计金额）'])
        baseline=result.get('comparison_refs',{}).get(comparison,{})
        def safe(value):
            value = str(value or '')
            return "'"+value if value.startswith(('=','+','-','@','\t','\r')) else value
        def managed_values(row):
            return [None if row['managed_metrics'] is None else row['managed_metrics'].get(key) for key in operations.DISPLAY]
        rounding_note='；'.join(f"{item['label']}：全范围显示合计{item['displayed_scope_total']}，逐项显示合计{item['displayed_rows_total']}，显示尾差{item['difference']}" for item in result['display_rounding'].values())
        for index,row in enumerate(sort_rows(result['rows'],sort_key,sort_direction,sort_basis)):
            changed=[] if comparison=='current' else [value for key in operations.DISPLAY for value in (row[comparison][key]['delta'],row[comparison][key]['rate'],{'zero_base':'上期为零','negative_base':'上期为负','unavailable':'缺少可比数据','comparable':'可比较'}[row[comparison][key]['state']])]
            duty='未认领' if row['unclaimed'] else '做货' if level in {'person','team'} or split=='person' else ''
            writer.writerow([result['version'],period,result['input_sha'],result['scope_sha'],'归属业绩',level,safe(row['name']),duty,safe(row['team']),safe(row['platform']),safe(row.get('store')),safe(row['store_id']),safe(row['person_id']),safe(row['product_id']),row['rank'] or '',row['order_count'],*[row['metrics'].get(key) for key in operations.DISPLAY],row['metrics'].get('commission'),';'.join(map(str,row['run_ids'])),baseline.get('period',''),baseline.get('input_sha',''),*changed,*managed_values(row),'原始精度汇总，界面两位显示；尾差不调整入账' if index==0 else '',rounding_note if index==0 else ''])
        for row in result['reference_groups']:
            writer.writerow([result['version'],period,result['input_sha'],result['scope_sha'],'抽点参考（不计业绩）',level,safe(row['person']),'抽点',safe(row['team']),safe(row['platform']),safe(row.get('store')),safe(row['store_id']),safe(row['person_id']),safe(row['product_id']),'',row['order_count'],*[row['metrics'].get(key) for key in operations.DISPLAY],row['metrics'].get('commission'),';'.join(map(str,row['run_ids'])),'','',*['']*len(comparison_headers),*managed_values(row),'',''])
        name='person-store' if split=='person' else level
        return Response('\ufeff'+out.getvalue(),media_type='text/csv; charset=utf-8',headers={'Content-Disposition':f'attachment; filename="operations-{period}-{name}.csv"','X-Operations-Input':result['input_sha'],'X-Operations-Scope':result['scope_sha']})

    @router.get('/evidence')
    def evidence(period:str,store_id:str,product_id:str='',order_id:str='',sub_order_id:str='',metric:str='',expected_input_sha:str='',offset:int=Query(0,ge=0),limit:int=Query(50,ge=1,le=200)):
        result = report(period,dict(level='store',store_id=store_id),False,expected_input_sha)
        ws, current_model = workspace(), model()
        ref = next((row for row in result['run_refs'] if row['store_id']==store_id),None)
        if ref is None or ref['run_id'] is None:
            raise HTTPException(404,'本店本月没有可回查的核算结果')
        try:
            from .storage_integrity import verified
            projection=operations.verified_run_path(ws,ref['run_id'],'.operations.parquet')
            complete=projection is not None
            if not complete:
                projection=operations.verified_run_path(ws,ref['run_id'],'.allocation.parquet')
            selected=pl.scan_parquet(projection) if projection is not None else None
            if selected is not None:
                schema=selected.collect_schema()
                for name,value in (('product_id',product_id),('order_id',order_id),('sub_order_id',sub_order_id)):
                    if value:
                        selected=selected.filter(pl.col(name).cast(pl.String)==value) if name in schema else selected.limit(0)
            components=operations._components(current_model)
            if metric and metric not in components:
                raise HTTPException(400,'不支持的经营来源科目')
            allowed = components.get(metric) if metric else None
            path = operations.verified_run_path(ws,ref['run_id'])
            if path is None:
                raise HTTPException(409,'原始事实证据尚未完整留存，暂不能展示依据')
            raw=pl.scan_parquet(path).filter(pl.col('counted'))
            if allowed is not None:
                raw=raw.filter(pl.col('metric_id').is_in(sorted(allowed)))
            if product_id or order_id or sub_order_id:
                if selected is None:
                    raw=raw.limit(0)
                else:
                    raw=raw.join(selected.select('metric_id','link_key').unique(),on=['metric_id','link_key'],how='semi',nulls_equal=True)
            total=raw.select(pl.len()).collect().item()
            columns=[name for name in ('metric_id','source_id','order_id','sub_order_id','link_key','file_sha','file_name','sheet','row_no','amount','contribution','source_note','counted') if name in raw.collect_schema()]
            items=raw.slice(offset,limit).select(columns).collect().to_dicts()
            allocations=[] if selected is None else selected.limit(200).collect().to_dicts()
            return {'version':result['version'],'period':period,'store_id':store_id,'run_id':ref['run_id'],
                'input_sha':result['input_sha'],'scope_sha':operations.digest([result['input_sha'],store_id,product_id,order_id,sub_order_id,metric]),'total':total,'items':items,
                'allocation':[{name:row.get(name) for name in ('spine_row','product_id','order_id','sub_order_id','metric_id','amount','factor','link_key')} for row in allocations],
                'evidence_complete':complete,'issues':result['issues']}
        except (OSError,ValueError,pl.exceptions.PolarsError) as exc:
            raise HTTPException(409,'经营依据读取失败，请检查核算留档') from exc

    app.include_router(router)
    return router
