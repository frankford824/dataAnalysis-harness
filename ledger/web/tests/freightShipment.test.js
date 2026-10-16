import {test} from 'node:test'
import assert from 'node:assert/strict'
import {amountText,canStart,identityLabel,memberRows,percentage,quantityLabel,trialState} from '../src/freightShipment.js'

test('nonzero subcent calculation values are not misleadingly displayed as zero',()=>{
  assert.equal(amountText('0.003333'),'<¥0.01')
  assert.equal(amountText('-0.003333'),'−<¥0.01')
  assert.equal(amountText(0),'¥0.00')
  assert.equal(amountText(null),'尚未形成')
})

test('proposal states are not labeled as booked money or a completed settlement',()=>{
  assert.equal(trialState({state:'ready'}),'试算已生成')
  assert.equal(trialState({state:'failed'}),'试算未完成')
  assert.equal(canStart({state:'running'}),false)
  assert.equal(canStart({state:'queued'}),false)
  assert.equal(canStart({state:'failed'}),true)
})
test('missing factors stay unknown while explicit zero is displayed as zero',()=>{
  assert.equal(percentage(null),'—')
  assert.equal(percentage(0),'0.00%')
  assert.equal(percentage('.666666666666'),'66.67%')
})
test('a pending child does not acquire a fabricated zero allocation',()=>{
  const detail={charge:{packet:{members:[{target_key:'a'},{target_key:'b'}]},allocations:[{target_key:'a',amount:'0',factor:'0'}]}}
  const rows=memberRows(detail)
  assert.equal(rows[0].allocation.amount,'0')
  assert.equal(rows[1].allocation,null)
})
test('native and replacement item identities are not mislabeled as platform child ids',()=>{
  assert.equal(identityLabel({identity_kind:'native_item'}),'平台商品行')
  assert.equal(identityLabel({identity_kind:'erp_reship_item'}),'ERP补发商品行')
  assert.equal(quantityLabel({quantity_verified:false,shipped_quantity:2}),'数量未用于分摊')
  assert.equal(quantityLabel({quantity_verified:true,shipped_quantity:1,ordered_quantity:2}),'本次 1 / 原数量 2')
})
