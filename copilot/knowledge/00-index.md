# 系统地图（先看这里）
章节（read_knowledge 传关键词）：glossary 业务口径与公式｜api 接口字段与耗时｜pages 页面↔接口｜data 库表｜rules 规则与老板决定｜issues 已知问题｜code 代码位置。

## 铁律
- ledger_get 结果截断 6000 字；大接口用 python：`d=get('/api/...',period='2026-09')` 只打印要的字段。
- 别调 /api/overview、/api/automation/months/*、/api/stores/{id}（>60s）；店铺列表用 /api/navigation。
- 金额 Decimal 字符串。trend/statement 里成本为负；operations/month 里成本为正。
- trend 每格 `{value,stores}`：stores=参与店数，缺数的店不计入→是“部分合计”，必须报店数。operations summary 任一店缺数即 null。
- 已结账月看冻结 run（period.run_id）；未结看最新 run。未知≠0，不得补零。
- 现状（11-03）：05/06/07 已结 40/42/43 家；08 起全开放；09 起缺运费→利润为空；09/10 兼职未填。详见 issues。
- 两种毛利：损益表 gross_profit 不扣退款；看板“商品毛利”=到账收入−商品成本。

## 常见问题快速路径
| 问题 | 调用 | 看哪些字段 |
|---|---|---|
| 全公司某月收入/毛利/利润 | /api/trend | rows[id=n_receipt/g_revenue/gross_profit/net_profit].cells[月].value+.stores |
| 同上（看板口径、排名） | /api/operations/month?period=&limit=60 | summary.net_income/gross_profit/profit/profit_after_labor；rows[].metrics；issues |
| 某店某月利润构成 | /api/operations/month?period=&store_id= （2s）；细到科目再 /api/stores/{id}/periods/{p}（~35s） | rows[0].metrics；statement[]{id,name,value,available,unavailable_reason} |
| 哪些店没结账、为什么 | /api/gaps?period= | cells[]{store,state,worst,gaps[]{kind,title,detail,source}}；硬原因见店月 close_verification.issues[]{code,message,hard} |
| 缺哪些资料 | 同上 gaps kind=missing；店月 sources[]{name,arrived,reason}、missing_sources | |
| 订单/商品查询 | /api/search?q=订单号或商品ID&limit=20 | hits[]{store,period,metric,amount,file,row_no} |
| 某科目明细 | /api/runs/{run_id}/drill/{node_id}?limit=20 | by_subject、by_file、sample、uncounted |
| 成本覆盖率 | 店月 cost_coverage{coverage,threshold,covered,expected}；缺口 /api/runs/{run}/coverage-gaps | category_counts、items |
| 提成 | /api/commission-v2/payout?store_id=&period= | stores[].total/amount_complete, people[]{person,amount} |
| 兼职工资/公摊 | /api/commission-v2/labor?period= | amount(null=未填)，rows[]{store,sales,share,amount} |
| 运费 | /api/freight-review?period= ；trend n_freight | stores[]{shown_amount,shown_reason} |
| 退款 | trend n_refund；ops metrics.refunds/refund_rate；drill n_refund | |
| 平台费用 | trend g_platform 及子项 n_software/n_marketing…；ops platform_cost | |
| 自动核算进度 | /api/recompute/progress；/api/automation/workers/status；/api/stores/{id}/pricing-status?period= | items[]{store,state,percent} |
| 自动结账记录 | 工具 auto_close_status | |

run_id 来源：ops run_refs[]{store_id,run_id,state,stale} 或店月 run_id。
