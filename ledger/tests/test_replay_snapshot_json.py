"""Snapshot diagnostics remain JSON-native; financial math stays exact."""
from decimal import Decimal
import json
import pytest
from ledger import replay_service as replay
from test_replay_service import complete_bundle,fixture_session,seed,complete_expected
from test_replay_rejections import gap_bundle,fixed_seed


@pytest.mark.parametrize('amount',['0','0.01','0.000001','-0.000001','12.123456','200.000001','999999999999.99'])
def test_snapshot_amount_preserves_exact_json_decimal_lexeme(amount):
    original=Decimal(amount);number=replay._snapshot_amount(original)
    raw=json.dumps({'amount':number},allow_nan=False)
    restored=json.loads(raw,parse_float=Decimal,parse_int=Decimal)
    assert isinstance(number,(int,float)) and restored['amount']==original
    if original:assert restored['amount']!=0


@pytest.mark.parametrize('value',[Decimal('NaN'),Decimal('Infinity'),Decimal('-Infinity'),float('nan'),float('inf'),True,False,'bad',
    Decimal('1e999999999'),Decimal('1e-999999999'),Decimal('9007199254740991.000001'),Decimal('0.123456789012345678901')])
def test_snapshot_amount_rejects_nonfinite_underflow_and_loss_instead_of_zero(value):
    with pytest.raises(replay.ReplayError):replay._snapshot_amount(value)


def test_snapshot_none_remains_unknown():
    assert replay._snapshot_amount(None) is None
    assert json.loads(json.dumps({'amount':replay._snapshot_amount(None)}))=={'amount':None}


def test_complete_actual_service_snapshot_plain_json_roundtrip(complete_bundle):
    path,_=complete_bundle
    with fixture_session(path,seed()) as session:
        session.recompute('s1');session.recompute('s2');session.drain_pending()
        snapshot=session.snapshot()
        decoded=json.loads(json.dumps(snapshot,allow_nan=False),parse_float=Decimal)
        for observation in decoded['observations'].values():
            company=observation['company']
            assert company['total']==0 and company['basis_total']==400 and company['known_income_subtotal']==400
            assert company['income_complete'] is True
        assert session.check(complete_expected())['status']=='passed'


def test_incomplete_actual_service_snapshot_keeps_subtotal_separate_from_missing_denominator(tmp_path):
    bundle,_=gap_bundle(tmp_path,store_count=2,overhead=False)
    with replay.Session(bundle,fixed_seed(),publication_scopes=['s1/2026-09','s2/2026-09']) as session:
        session.recompute('s1')
        snapshot=session.snapshot()
        decoded=json.loads(json.dumps(snapshot,allow_nan=False),parse_float=Decimal)
        for observation in decoded['observations'].values():
            company=observation['company']
            assert company['total'] is None and company['basis_total'] is None
            assert company['income_complete'] is False and company['known_income_subtotal']==200
            assert company['missing_store_ids']==['s2']
