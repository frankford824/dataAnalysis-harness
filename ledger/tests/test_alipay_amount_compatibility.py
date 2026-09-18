import csv
import io

import pytest
from conftest import MODELS
from ledger.model.loader import load_model
from ledger.engine.runtime import ingest
from ledger.service import _attach_parse_failures


@pytest.mark.parametrize('income,outgo', [('收入金额','支出金额'),('收入金额（+元）','支出金额（-元）'),('收入金额(+元)','支出金额(-元)')])
@pytest.mark.parametrize('encoding,leading', [('utf-8-sig',False),('gb18030',True)])
def test_alipay_real_export_shapes_preserve_money(tmp_path,income,outgo,encoding,leading):
    model=load_model(MODELS/'cn-ecommerce')
    headers=['账务流水号','业务流水号','业务描述','业务基础订单号','商户订单号','发生时间',income,outgo,'业务类型','备注']
    rows=[['tx1','b1','交易收款','123456789012345678','123456789012345678','2026-06-12 12:00:00','123.45','0','交易',''],
          ['tx2','b2','交易退款','123456789012345678','123456789012345678','2026-06-13 12:00:00','0','-23.45','退款','退款']]
    text=io.StringIO();text.write('#支付宝账务明细查询\n#账户说明\n#导出日期\n#账单说明\n')
    writer=csv.writer(text)
    writer.writerow(([''] if leading else [])+headers)
    writer.writerows(([''] if leading else [])+row for row in rows)
    path=tmp_path/'对账支付宝-测试店-6月.csv';path.write_bytes(text.getvalue().encode(encoding))
    parsed=ingest([path],model,default_store='测试店',cache_root=None)
    assert len(parsed.known)==1,[(i.error,i.notes) for i in parsed.items]
    item=parsed.known[0]
    assert item.template.id=='taobao_settlement_alipay_v1'
    assert item.frame['income'].sum()==pytest.approx(123.45)
    assert item.frame['outgo'].sum()==pytest.approx(-23.45)


def test_failed_file_cannot_be_hidden_by_successful_channel():
    payload={'can_close':True,'sources':[{'id':'settlement','arrived':True}],
             'findings':[],'commission':{'amount_complete':True,'notes':[]}}
    _attach_parse_failures(payload,[{'file':'对账支付宝-6月.csv','reason':'金额列缺失'}])
    assert payload['can_close'] is False
    assert payload['commission']['amount_complete'] is False
    assert payload['file_errors'][0]['file']=='对账支付宝-6月.csv'
    assert payload['findings'][0]['blocking']
    assert '对账支付宝-6月.csv' in payload['findings'][0]['message']
