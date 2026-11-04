# 数据与库表
目录：程序 D:\ledger\app（台账代码 D:\ledger\app\ledger）；数据 LEDGER_HOME=D:\ledger\home；日志 D:\ledger\logs；助手 D:\ledger\agent，助手数据 D:\ledger\home\agent；虚拟环境 D:\ledger\venv。
NAS：X:\台账系统（00_上传区 / 10_已接收\平台\店名 [store_id]\类别\文件 / 20_需修正 / 90_历史版本 / 99_系统）。
模型（规则）：D:\ledger\app\models\cn-ecommerce（statement/metrics/checks/sources.yaml、dictionary.csv 科目字典、stores.yaml、overheads.csv、commission.csv）。

## sql 工具
`sql(query, db='workspace'|'commission')` 只读；60 秒中止、最多 200 行。workspace.db 100GB+：**大表必须按 store_id/period 过滤并 LIMIT，不要 SELECT run.result**（巨大，且可能是快照引用 {"$ledger_snapshot_v1":sha}，在 evidence_blob 里 zlib 压缩，SQL 解不开 → 用 API）。

## workspace.db（D:\ledger\home\workspace.db，WAL）
- period(store_id,period,state open|closed,changed_at,by,note,run_id 冻结结果,at_version) pk(store_id,period)。没有行=从未结账（开放）。
- run(id,store_id,period,at,can_close,evidence_ready,evidence_error,engine,model_revision,input_fingerprint,shas,result) 索引 run_key(store_id,period,id desc)。约 25 万行。
- run_report_slice(run_id,payload_kind 'run'|'manual',statement_json 纯 JSON[{id,value,available}],commission_json,store_name,overview_json)：取损益表数的快捷方式（可能缺失，缺则走 API）。
- manual_finance(run_id pk,result_json,decision_json,at,by) 人工结账结果，不可改。
- run_labor(run_id pk,store_id,period,amount) 结账时冻结的兼职分摊，不可改。
- cost_line_log(store_id,period,coverage_key,amount,action save|remove,reason,at,by) 人工补录成本，只追加。
- version(id,store_id,name,sha,at,by) 每次上传一版；slot(store_id,name,sha) 当前生效文件（同名替换）；file(sha,name,size)。
- config_log(at,by,kind,summary,before_json,after_json) 配置改动审计。
- automation_case(store_id,period,status,stage,issues_json,run_id) 自动化进度；automation_decision(period,store_id,kind,values_json,reason,action)；automation_proposal / receipt / overhead_close(period,total,shares_json) / authorization；agent_question 员工异议。
- 其他库：order-feed.db（订单台同步）、storage.db、sentinel.db（哨兵）。

## 常用 SQL
各店某月状态与冻结 run：
`SELECT store_id,state,run_id,changed_at,by FROM period WHERE period='2026-07'`
某店某月最新 run：
`SELECT id,at,can_close,engine FROM run WHERE store_id=? AND period=? ORDER BY id DESC LIMIT 1`
各店某月损益项（已结取冻结 run，未结取最新 run）：
```
WITH r AS (SELECT store_id,period,max(id) run_id FROM run WHERE period='2026-07' GROUP BY store_id),
x AS (SELECT r.store_id,COALESCE(p.run_id,r.run_id) run_id,COALESCE(p.state,'open') state
      FROM r LEFT JOIN period p ON p.store_id=r.store_id AND p.period=r.period)
SELECT x.store_id,x.state,json_extract(j.value,'$.id') id,json_extract(j.value,'$.value') v,json_extract(j.value,'$.available') ok
FROM x JOIN run_report_slice s ON s.run_id=x.run_id AND s.payload_kind='run', json_each(s.statement_json) j
WHERE json_extract(j.value,'$.id') IN ('n_receipt','gross_profit','net_profit')
```
（人工结账的店 payload_kind='manual'；结果以 API 为准，SQL 用于快速核对。）
结账/反结账记录：`SELECT * FROM config_log ORDER BY id DESC LIMIT 20`。

## commission/registry.db（db='commission'）
- person(id,name,employee_no,archived,parent_id 上级,alias,default_cut_rate) 约 39 人；org_store(person_id,store_id)。
- store_member(store_id,person_id,valid_from,valid_to,duty produce|cut,leader_id) 按时间段的店铺身份。
- scheme(id,store_id,product_id,product_name,active_version,draft_version) 约 12 万；scheme_version 不可改（规则、点数、生效时间）。
- catalog(store_id,product_id,product_name,order_store_id) 商品目录。
- calculation(finance_run,store_id,period,summary_json…) 提成计算记录；settlement 结算；payout_confirmation(store_id,period,finance_run,payouts_json,confirmed_total) 核定实发（不可改）；profit_exclusion 不提成订单。
- pending(store_id,revision,next_attempt,error) 提成重算队列；job(kind,status) 后台任务；event 审计。

## 外部服务
- 订单台 8001（订单/成本/售后实时数据，PostgreSQL orderdb）：LEDGER_ORDER_FEED_URL http://127.0.0.1:8001/api/integration/ledger/v1，交换目录 D:\order\exchange\ledger-feed。台账成本表/售后单会被订单台数据替换。
- 索引器 8765（全文检索 /api/search 背后），目录 D:\ledger\index\catalog.db。
- 助手 8020，台账把 /api/agent/* 转发给它。

## 日志
台账 D:\ledger\logs\serve-YYYYMMDD.log；助手 agent-YYYYMMDD.log；保留 30 天。查报错：powershell `Select-String -Path D:\ledger\logs\serve-$(Get-Date -f yyyyMMdd).log -Pattern 'Traceback|ERROR' | Select -Last 20`。
