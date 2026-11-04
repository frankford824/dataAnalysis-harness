# 页面地图（前端 ledger/web，hash 路由：http://192.168.0.155:8000/#/路径）
布局：左侧 .dock-main 为业务页，右侧 AgentPanel 是台账助手。左栏导航在 components/AppBody.vue。
用户说“这个页面/这个数”时，消息里会带当前页面、月份、店铺和筛选条件；按下表找对应接口复现。

| 路由 | 页面（文件） | 用途 | 主要接口 |
|---|---|---|---|
| / | 月度工作台 BoardView.vue | 全公司/团队/人/商品/订单经营汇总，结账批次，待你确认 | /api/operations/month(level, sort)、/api/operations/evidence、导出；/api/automation/months/{p}（慢，15s 轮询）；close-batches preview/confirm；decisions；/api/automation/inbox |
| /deliver | 数据与店铺 DeliverView.vue | 上传资料、店铺登记、源文件解析错误、订单台同步 | /api/stores(add)、上传、/api/index/errors|files|storage|preview、/api/order-feed/status |
| /store/:id?period= | 店月详情 PeriodView.vue | 损益表、检查项、缺口、成本核价、结账/反结账 | /api/stores/{id}/periods/{p}、/api/stores/{id}/recompute、close/reopen、manual-cost preview、/api/runs/{run}/fees.csv |
| /freight | 运费复核 FreightReview.vue | 展示运费 vs 试算运费，按店分组 | /api/freight-review(+export.csv)、/api/freight-review/shipments* |
| /fees | 费项 FeesView.vue | 科目字典：资金流水科目→损益项归类（已有归类/已配规则/改动记录） | /api/fees、/api/fees/preview、POST /api/fees、/api/fees/suggest、/api/fees/jobs |
| /commission | 提成设置 CommissionWorkspace.vue | 店铺×商品提成规则、导入、未分配 | /api/commission-v2/settings、settings/count、preview、import-preview、unassigned |
| /commission/reports | 金额汇总 CommissionReports.vue | 提成报表、结算、逐人确认实发 | /api/commission-v2/reports/query、settlements、payout-confirmations、sales-attribution/audit、profit-composition |
| /commission/org | 组织架构 CommissionOrgTree.vue | 团队-组-人树、店铺归属、人员档案 | /api/commission-v2/org/tree、org/stores、org/move、org/person、org/infer、people |
| /commission/legacy | 旧提成页 CommissionView.vue | 旧版 | /api/commission |
| /labor | 兼职人工费用 LaborView.vue | 录入当月兼职总额，按各店销售收入占比分摊 | GET/POST /api/commission-v2/labor |
| /ops | 运维状态 OpsView.vue | 哨兵状态、通知、事件 | /api/operations/sentinel/status|notifications|events |
| /onboard/:sha | 新表头识别 OnboardView.vue | 新格式源表登记 | /api/index/preview 等 |

## 店月详情页的组成（PeriodView 子组件）
- StatementReview：损益表行（= statement[]），点行 → DrillDrawer（/api/runs/{run}/drill/{node}、/api/search）。
- PricingPending：成本待核价/覆盖率缺口（pricing-status、pricing-gaps、coverage-gaps(.xlsx)、cost-lines 保存/批量）。
- OrderIssuesDrawer：订单问题（order_issue_counts）。
- AllocationNotice：共享表分摊待核对（allocation.csv）。
- PeriodCostApproval：整期成本人工批准。
- AgentQuestions：员工对某笔的异议（/api/automation/questions）。
- 结账按钮受 close_verification.allowed 控制；人工结账弹窗会带入试算提成。

## 月度工作台要点
- 两个 tab：results 经营结果 / decisions 待决定；level 切换 店铺/团队/人员/商品/订单。
- 核心列：net_income、goods_cost、gross_profit、promotion_cost、platform_cost、freight_cost、aftersales_cost、labor_cost、profit_after_labor（默认按它降序）。
- 状态词：completed 完成 / processing 处理中 / needs_decision 需决定。
- 组件 AgentAuthorityPanel（一次同意长期自动处理/停止）、AgentInbox（新版本核对+员工异议）、CompanyFinanceCard（公司汇总）。

## 前端构建
web 源码 ledger/web（Vue3 + naive-ui + vite），`pnpm build` 输出到 ledger/ledger/static，台账服务直接托管。助手用 build_web 工具构建（有快照可撤销），不要手工覆盖 static。
