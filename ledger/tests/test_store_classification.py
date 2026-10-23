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
    assert result['matched_count']==51 and len(result['unmatched'])==64
    assert 'douyin_mt9s1onx' not in result['mapping']
    assert next(r for r in result['unmatched'] if r['source_row']==29)['reason']=='平台登记不一致'
    assert result['mapping']['pdd_mt9so37w']=='festival'  # Confirmed ERP ID survives the operator rename.


def test_missing_or_changed_registered_identity_never_receives_a_category():
    result=classification.resolve([{'id':'other_yongbo','platform':'pdd'}])
    assert not result['mapping']
    assert next(r for r in result['unmatched'] if r['source_row']==105)['reason']=='登记平台已变化，需重新核对'


def test_only_category_and_exclusion_keep_unknowns_and_mixed_categories_separate():
    cat={'mapping':{'f1':'festival','f2':'festival','p1':'foil_paint'},'categories':[{'id':'festival'},{'id':'foil_paint'}]}
    rows=pl.DataFrame({'id':[1,2,3,4,5,6],'store_ids':[['f1'],['p1'],[],['f1','p1'],['f1','unknown'],['f1','f2']]}).lazy()
    assert classification.filter_rows(rows,cat,only_category='festival').collect()['id'].to_list()==[1,6]
    assert classification.filter_rows(rows,cat,exclude_categories='foil_paint').collect()['id'].to_list()==[1,3,4,5,6]
    assert classification.filter_rows(rows,cat,only_category='festival',classification_view='unmatched').collect()['id'].to_list()==[3,4,5]
    with pytest.raises(InputError):classification.filter_rows(rows,cat,only_category='festival',exclude_categories='festival')
    with pytest.raises(InputError):classification.filter_rows(rows,cat,only_category='invented')
