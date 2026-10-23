from collections import Counter
import polars as pl
import pytest

from ledger import store_classification as classification
from ledger.freight_trial_inputs import InputError


def test_supplied_catalog_preserves_every_source_row_and_does_not_merge_duplicate_names():
    source=classification.source()
    assert source['source_sha256']=='d4e1489372ec59da2a7cbe318ee59de9873ed6be2860033af5f2f3cc1c15ed7b'
    assert [r['source_row'] for r in source['rows']]==list(range(2,117))
    counts=Counter(r['category_id'] for r in source['rows'])
    assert counts['festival']==49 and counts['foil_paint']==28 and len(counts)==11
    duplicate=[r for r in source['rows'] if r['source_row'] in (61,63)]
    assert len(duplicate)==2 and all(not r['ledger_store_id'] for r in duplicate)
    assert len({r['name'] for r in duplicate})==2
    stores=[{'id':r['ledger_store_id'],'platform':r['ledger_platform']} for r in source['rows'] if r['ledger_store_id']]
    result=classification.resolve(stores)
    assert result['matched_count']==60 and len(result['unmatched'])==55
    assert result['ledger_matched_count']==51 and len(result['erp_only'])==9
    assert result['erp_mapping']['19027942|得物']=='festival'
    assert 'douyin_mt9s1onx' not in result['mapping']
    assert next(r for r in result['erp_only'] if r['source_row']==29)['reason']=='平台登记不一致'
    assert result['mapping']['pdd_mt9so37w']=='festival'  # Confirmed ERP ID survives the operator rename.


def test_missing_or_changed_registered_identity_never_receives_a_category():
    result=classification.resolve([{'id':'other_yongbo','platform':'pdd'}])
    assert not result['mapping']
    assert next(r for r in result['erp_only'] if r['source_row']==105)['reason']=='登记平台已变化，需重新核对'


def test_only_category_and_exclusion_keep_unknowns_and_mixed_categories_separate():
    cat={'mapping':{'f1':'festival','f2':'festival','p1':'foil_paint'},'categories':[{'id':'festival'},{'id':'foil_paint'}]}
    rows=pl.DataFrame({'id':[1,2,3,4,5,6],'store_ids':[['f1'],['p1'],[],['f1','p1'],['f1','unknown'],['f1','f2']]}).lazy()
    assert classification.filter_rows(rows,cat,only_category='festival').collect()['id'].to_list()==[1,6]
    assert classification.filter_rows(rows,cat,exclude_categories='foil_paint').collect()['id'].to_list()==[1,3,4,5,6]
    assert classification.filter_rows(rows,cat,only_category='festival',classification_view='unmatched').collect()['id'].to_list()==[3,4,5]
    with pytest.raises(InputError):classification.filter_rows(rows,cat,only_category='festival',exclude_categories='festival')
    with pytest.raises(InputError):classification.filter_rows(rows,cat,only_category='invented')


def test_erp_identity_classifies_without_a_ledger_store_but_blocks_partial_and_conflicting_evidence():
    catalog={'mapping':{'foil':'foil_paint','festival':'festival'},'erp_mapping':{'19027942|得物':'festival','12876922|其它平台':'foil_paint'}}
    rows=pl.DataFrame({'id':[1,2,3,4,5,6,7,8],
        'store_ids':[[],[],['festival'],['festival'],['foil'],['festival'],[],['festival']],
        'erp_shop_keys':[['19027942|得物'],['12876922|其它平台'],['19027942|得物',''],['unknown|得物'],
                         ['19027942|得物'],[],['19027942|抖音'],['19027942|得物','12876922|其它平台']]}).lazy()
    result=classification.annotate(rows,catalog).collect()
    assert result['store_category'].to_list()==['festival','foil_paint','__unmatched__','__unmatched__','__unmatched__','festival','__unmatched__','__unmatched__']


def test_unmatched_export_lists_all_55_with_missing_evidence_and_does_not_assert_nonexistence():
    import csv,io
    result=classification.resolve([])
    rows=list(csv.DictReader(io.StringIO(classification.unmatched_csv(result,result['version']).lstrip('\ufeff'))))
    assert len(rows)==55 and len({r['原表行'] for r in rows})==55
    assert all('聚水潭店铺ID' in r['缺少的依据'] and '当前聚水潭查询未返回' in r['当前核查结果'] for r in rows)
    assert '同店曾用名证明' in next(r for r in rows if r['原表行']=='61')['缺少的依据']
    assert '欢喜节日礼铺' in {r['店铺名称'] for r in rows}
    with pytest.raises(InputError,match='更新'):classification.unmatched_csv(result,'stale-version')
