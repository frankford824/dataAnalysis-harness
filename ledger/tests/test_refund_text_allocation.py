"""Read real export headers before checking ratios; direct-frame tests miss parsing loss."""
import polars as pl
import pytest

from conftest import MODELS, write_xlsx
from ledger.engine.runtime import ingest
from ledger.engine.project import project
from ledger.engine.link import Spine
from ledger.engine.normalize import to_number
from ledger.engine.allocation import prepare
from ledger.model.loader import load_model
from ledger.model.schema import Allocation
from test_allocation import _facts, _metric


def parsed(tmp_path, payments, refunds):
    model = load_model(MODELS / 'cn-ecommerce')
    headers = ['子订单编号','主订单编号','买家实付金额','退款金额','订单创建时间','商品ID','物流单号']
    rows = [[f'A{i}', 'A', paid, refund, '2026-07-30 13:34:24', f'P{i}', 'T1']
            for i,(paid,refund) in enumerate(zip(payments,refunds))]
    path = write_xlsx(tmp_path / '订单明细.xlsx', [['主单金额按净实付比例分配'],headers,*rows],sheet='export')
    before = path.read_bytes()
    ing = ingest([path],model,default_store='测试店')
    assert path.read_bytes() == before
    record = ing.frames_of('order_detail')[0]
    assert record.recognition.template_id == 'taobao_order_detail_v1'
    return record.frame.with_columns(pl.lit('测试店').alias('store'),pl.lit('2026-07').alias('period'),
                                    pl.lit('order_detail_file').alias('__spine_origin__')),record.notes


@pytest.mark.parametrize('paid,refund,source_amount,expected', [
    ([30.90,33.,24.20],['无退款申请']*3,88.1,[30.90,33.,24.20]),
    ([22.4,33.,36.6],['无退款申请']*3,92.,[22.4,33.,36.6]),
    ([5.22,25.82],['无退款申请',26.82],5.22,[5.22,0.]),
    ([70.,30.],['￥20.00','无退款申请'],80.,[50.,30.]),
])
def test_export_to_allocation_uses_explicit_no_refund_as_zero(tmp_path, paid, refund, source_amount, expected):
    frame,notes = parsed(tmp_path,paid,refund)
    assert frame['refund_amount'].null_count()==0
    assert not any('refund_amount 有' in note for note in notes)
    got=project(_facts([('A',source_amount)]),_metric(Allocation(mode='ratio',by='alloc_ratio')),Spine(frame))
    assert not got.allocation_pending
    assert got.facts['amount'].to_list()==pytest.approx([amount for amount in expected if amount],abs=1e-6)
    weights=prepare(frame.with_columns(pl.col('order_id').alias('link_key')),
                    _metric(Allocation(mode='ratio',by='alloc_ratio')))
    assert weights['__allocation_factor'].to_list()==pytest.approx([amount/source_amount for amount in expected])
    assert got.facts['amount'].sum()==pytest.approx(source_amount,abs=1e-6)
    assert got.facts['factor'].sum()==pytest.approx(1.)


@pytest.mark.parametrize('unknown',[None,'','待核实','-'])
def test_missing_or_unrecognized_refund_stays_pending(tmp_path, unknown):
    frame,_=parsed(tmp_path,[70.,30.],[unknown,'无退款申请'])
    assert frame['refund_amount'].to_list()==[None,0.]
    got=project(_facts([('A',100.)]),_metric(Allocation(mode='ratio',by='alloc_ratio')),Spine(frame))
    assert got.allocation_pending[0]['reason']=='missing_payment_basis'
    assert got.facts['spine_row'].null_count()==got.facts.height
    assert got.facts['amount'].sum()==100.


def test_all_refunded_fee_shares_equally_but_income_and_generic_parser_do_not_change(tmp_path):
    frame,_=parsed(tmp_path,[5.,10.],[5.,10.])
    got=project(_facts([('A',3.)]),_metric(Allocation(mode='ratio',by='alloc_ratio')),Spine(frame))
    assert not got.allocation_pending
    assert got.facts['amount'].to_list()==[1.5,1.5]
    assert got.facts['allocation_method'].to_list()==['zero_net_equal_children']*2
    income=project(_facts([('A',3.)],metric_id='trade_receipt').with_columns(pl.lit('trade_receipt').alias('major')),
        _metric(Allocation(mode='ratio',by='alloc_ratio'),id='trade_receipt',source='settlement',major='trade_receipt'),Spine(frame))
    assert income.allocation_pending[0]['reason']=='zero_net_payment'
    assert to_number('无退款申请') is None
