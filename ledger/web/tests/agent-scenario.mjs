import assert from 'node:assert/strict'
import path from 'node:path'

const workerHeaders={Authorization:'Bearer browser-fixture-local-worker'}

async function workerAnswer(page,origin,text){
  const claimed=await page.request.post(origin+'/api/v1/agent-questions/claim',{headers:workerHeaders,data:{agent_id:'browser-worker'}})
  assert.equal(claimed.status(),200,await claimed.text())
  const body=await claimed.json()
  assert.equal(JSON.stringify(body.context).includes('"value"'),false,'Model-visible context carries no amounts')
  const node=body.context.statement.find(row=>row.available&&row.sign)
  assert.ok(node,'Fixture statement has an available line')
  const answer=await page.request.post(origin+`/api/v1/agent-questions/${body.question_id}/answer`,{headers:workerHeaders,data:{lease_token:body.lease_token,text:text(node)}})
  assert.equal(answer.status(),200,await answer.text())
  return {node,status:(await answer.json()).status}
}

export async function agentScenario(page,{origin,base,artifacts}){
  const errors=[]
  page.on('pageerror',e=>errors.push(e.message))
  page.on('console',e=>{if(e.type()==='error')errors.push(e.text())})
  await page.goto(base+'/#/store/s1?period=2026-06')
  await page.getByRole('button',{name:'问 Agent',exact:true}).click()
  const box=page.getByRole('textbox',{name:'向 Agent 提问'})
  await box.fill('本月这一项是怎么来的？')
  await page.getByRole('button',{name:'提问',exact:true}).click()
  await page.locator('[data-question-state="queued"]').waitFor()
  const first=await workerAnswer(page,origin,node=>`${node.name}为{{node:${node.id}}}，由本期核算引擎按原始资料计算。`)
  assert.equal(first.status,'answered')
  const answered=page.locator('[data-question-state="answered"]')
  await answered.waitFor({timeout:15000})
  const shown=await answered.innerText()
  assert.ok(shown.includes(first.node.name)&&!shown.includes('{{node:'),'Answer is rendered by the engine')
  await box.fill('能估算一下吗？')
  await page.getByRole('button',{name:'提问',exact:true}).click()
  await page.locator('[data-question-state="queued"]').waitFor()
  assert.equal((await workerAnswer(page,origin,()=>'大约是 100 元')).status,'rejected')
  await page.getByText('无法由账本核实的数字',{exact:false}).waitFor({timeout:15000})
  await page.screenshot({path:path.join(artifacts,'agent-questions-desktop.png'),fullPage:false})
  await page.keyboard.press('Escape')

  const asked=await page.request.post(origin+'/api/automation/questions',{data:{request_id:'browser-dispute',store_id:'s1',period:'2026-06',
    question:'这笔已经退款，不应计入',subject:{node_id:first.node.id,row:{metric_id:'m',file_name:'f.xlsx',sheet:'sheet',row_no:3,accounting_status:'已计入'}}}})
  assert.equal(asked.status(),200,await asked.text())
  const disputed=await workerAnswer(page,origin,node=>`这笔按原始资料计入${node.name}，合计{{node:${node.id}}}。`)
  assert.equal(disputed.status,'answered')

  await page.goto(base+'/#/')
  const card=page.locator('[data-inbox-card="dispute"]')
  await card.waitFor({timeout:15000})
  assert.ok((await card.innerText()).includes('这笔已经退款'),'Dispute is waiting in the inbox')
  await page.screenshot({path:path.join(artifacts,'agent-inbox-desktop.png'),fullPage:false})
  await card.getByRole('button',{name:'认可，已处理',exact:true}).click()
  await card.waitFor({state:'detached',timeout:15000})

  await page.getByRole('tab',{name:/需你决定/}).click()
  const previewed=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/automation/agent-authority/preview')
  const applied=page.waitForResponse(r=>new URL(r.url()).pathname==='/api/automation/agent-authority/apply')
  await page.getByRole('button',{name:'同意，长期自动处理',exact:true}).click()
  const preview=await (await previewed).json()
  assert.equal(preview.effect.closed_store_months_changed,false);assert.equal(preview.effect.standing,true)
  assert.equal(preview.effect.grant_count,preview.effect.stores.length*preview.effect.rules.length)
  assert.equal((await applied).status(),200)
  await page.getByRole('heading',{name:'Agent 自动处理 · 长期自动处理中'}).waitFor()
  const period=preview.effect.period_from
  const granted=await (await page.request.get(origin+`/api/automation/agent-authority?period=${period}`)).json()
  assert.equal(granted.active,true);assert.equal(granted.standing.active,true);assert.equal(granted.delegated,preview.effect.grant_count)
  await page.screenshot({path:path.join(artifacts,'agent-authority-desktop.png'),fullPage:false})
  await page.getByRole('button',{name:'停止自动处理',exact:true}).click()
  await page.getByRole('button',{name:'确认停止',exact:true}).click()
  await page.getByRole('heading',{name:'Agent 自动处理 · 未开启'}).waitFor()
  const withdrawn=await (await page.request.get(origin+`/api/automation/agent-authority?period=${period}`)).json()
  assert.equal(withdrawn.active,false);assert.equal(withdrawn.covered,0);assert.equal(withdrawn.standing.active,false)
  assert.deepEqual(errors.filter(e=>!/Failed to load resource/.test(e)),[])
  return `standing consent ${preview.effect.grant_count} grants -> stopped; dispute answered and closed from inbox; invented number rejected`
}
