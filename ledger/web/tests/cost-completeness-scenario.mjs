import assert from 'node:assert/strict'

export async function costCompletenessScenario(page, screenshotDirectory) {
  await page.getByRole('button',{name:'核对未完整成本',exact:true}).click()
  const goods=page.getByRole('textbox',{name:'确认商品成本金额',exact:true})
  await goods.waitFor()
  assert.equal(await goods.inputValue(),'','Known subtotal must not be a period-total default')
  assert.equal(await page.getByRole('textbox',{name:'确认代发成本金额',exact:true}).inputValue(),'0.00')
  assert.equal(await page.getByRole('textbox',{name:'确认补发成本金额',exact:true}).inputValue(),'0.00')
  assert.equal(await page.getByRole('textbox',{name:'合成人员确认提成额',exact:true}).inputValue(),'',
    'Partial payout trial must not become an approved-payment default')
  await page.getByText('已识别部分 38.00 元（总额待补齐）',{exact:true}).waitFor()
  assert.ok(await page.getByRole('button',{name:'预览确认金额',exact:true}).isDisabled())
  assert.ok(await page.getByRole('button',{name:'确认结账',exact:true}).isDisabled())
  assert.equal(await page.evaluate(()=>window.costRegression.closeCalls.length),0)
  if(screenshotDirectory){
    await page.evaluate(async()=>Promise.all(document.getAnimations().filter(a=>Number.isFinite(a.effect?.getTiming().iterations)).map(a=>a.finished.catch(()=>{}))))
    await page.screenshot({path:screenshotDirectory+'/cost-unknown-total.png',fullPage:false})
  }
  await page.getByRole('button',{name:'取消',exact:true}).click()
  await goods.waitFor({state:'hidden'})

  await page.getByRole('button',{name:'模拟补录已入账',exact:true}).click()
  await page.getByRole('button',{name:'结账 2026-09',exact:true}).waitFor()
  await page.getByRole('button',{name:'结账 2026-09',exact:true}).click()
  await page.waitForFunction(()=>window.costRegression.closeCalls.length===1)
  assert.equal(await page.getByRole('textbox',{name:'确认商品成本金额',exact:true}).count(),0,
    'Posted supplement must not reopen whole-period cost entry')
  const submitted=await page.evaluate(()=>window.costRegression.closeCalls[0])
  assert.ok(!submitted[5] || !submitted[5].costs,'No whole-period money is re-entered')
  await page.getByRole('button',{name:'反结账',exact:true}).waitFor()

  await page.getByRole('button',{name:'模拟补录待核算',exact:true}).click()
  await page.getByRole('button',{name:'查看结账条件',exact:true}).waitFor()
  await page.getByRole('button',{name:'查看结账条件',exact:true}).click()
  assert.equal(await page.getByRole('textbox',{name:'确认商品成本金额',exact:true}).count(),0)
  assert.equal(await page.evaluate(()=>window.costRegression.closeCalls.length),1)
  return 'unknown total stays blank; posted supplement needs no re-entry; pending calculation cannot close'
}
