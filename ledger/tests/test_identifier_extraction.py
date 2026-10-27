"""Identifier DSL compatibility with synthetic rows, no production model data."""
from datetime import date

import polars as pl
import pytest
from pydantic import ValidationError

from ledger.engine.normalize import normalize
from ledger.engine.types import ANCHOR_ROW,FileRef,RawRow,RawTable
from ledger.model.schema import ColumnBinding,IdentifierExtraction,Template


def template(**change):
    values={'target':'order_id','when':{'field':'subject','equals':['adjustment']},
        'from_role':'remark','extract':r'original:\s*(\d+)','value_pattern':r'\d{18}'}
    values.update(change)
    return Template(id='identifier-fixture',source='settlement',match_columns=('subject','order_id','remark'),
        bindings=tuple(ColumnBinding(role=name,columns=(name,),kind=kind) for name,kind in
            [('subject','text'),('order_id','text'),('remark','text'),('amount','number'),('settle_time','time')]),
        time_slots={'settle_date':'settle_time'},identifier_extractions=(IdentifierExtraction(**values),))


def normalize_rows(tpl,rows):
    headers=['subject','order_id','remark','amount','settle_time']
    return normalize(RawTable(FileRef('synthetic','input.csv','sheet'),headers,
        [RawRow(index+2,tuple(row)) for index,row in enumerate(rows)]),tpl)


def test_declared_scene_replaces_only_one_unambiguous_identifier_and_preserves_raw_money_and_day():
    frame,notes=normalize_rows(template(),[
        ['adjustment','party-a','original: 123456789012345678','3.335','2026-06-04'],
        ['adjustment','party-b','original: 123456789012345678 / original: 123456789012345678','4.00','2026-06-05'],
        ['adjustment','party-c','original: 123456789012345678 / original: 223456789012345678','5.00','2026-06-06'],
        ['adjustment','party-d','original: 123','6.00','2026-06-07'],
        ['adjustment','party-e',None,'7.00','2026-06-08'],
        ['ordinary','keep-me','original: 123456789012345678','8.00','2026-06-09'],
    ])
    assert frame['order_id'].to_list()==['123456789012345678','123456789012345678',None,None,None,'keep-me']
    assert frame['__raw_order_id'].to_list()==['party-a','party-b','party-c','party-d','party-e','keep-me']
    assert frame['amount'].to_list()==[3.335,4,5,6,7,8]
    assert frame['settle_date'].to_list()==[date(2026,6,day) for day in range(4,10)]
    assert frame[ANCHOR_ROW].to_list()==list(range(2,8))
    assert any('5 行，其中 3 行' in note for note in notes)
    assert not any(column.startswith('__identifier_extraction_selected__') for column in frame.columns)


def test_selection_counts_use_original_identifier_when_condition_targets_itself():
    tpl=template(when={'field':'order_id','matches':'^party-'})
    frame,notes=normalize_rows(tpl,[['ordinary','party-a','original: 123456789012345678','1.25','2026-06-04']])
    assert frame['order_id'][0]=='123456789012345678' and frame['__raw_order_id'][0]=='party-a'
    assert any('1 行，其中 0 行' in note for note in notes)


def test_python_only_matcher_and_multiple_replacements_keep_first_raw_identifier():
    tpl=template(when={'field':'subject','matches':'(?<=fix:)ok'})
    first=tpl.identifier_extractions[0]
    tpl=tpl.model_copy(update={'identifier_extractions':(first,first)})
    frame,_=normalize_rows(tpl,[['fix:ok','party-a','original: 123456789012345678','1.25','2026-06-04']])
    assert frame['order_id'][0]=='123456789012345678' and frame['__raw_order_id'][0]=='party-a'


@pytest.mark.parametrize('change',[
    {'target':'amount'},{'target':'settle_time'},{'extract':r'original:\s*\d+'},
    {'extract':r'(original:)\s*(\d+)'},{'extract':'('},{'value_pattern':'['},
])
def test_identifier_schema_cannot_rewrite_amount_or_day_or_accept_invalid_capture_contract(change):
    with pytest.raises(ValidationError):template(**change)


@pytest.mark.parametrize('field,value',[
    ('from_role','missing'),('target','sub_order_id'),('when',{'field':'missing','equals':['adjustment']}),
])
def test_template_contract_requires_all_identifier_roles_to_be_declared(field,value):
    with pytest.raises(ValidationError,match='未定义'):template(**{field:value})


def test_identifier_contract_round_trips_without_dropping_fields():
    tpl=template()
    loaded=Template.model_validate_json(tpl.model_dump_json())
    assert loaded.identifier_extractions==tpl.identifier_extractions
    frame,_=normalize_rows(loaded,[['adjustment','party-a','original: 123456789012345678','1.25','2026-06-04']])
    assert frame['order_id'][0]=='123456789012345678'
