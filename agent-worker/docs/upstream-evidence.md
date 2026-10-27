# 默认上游取证

`ledger_agent.upstream.Collector(config, root, *, client=None, order_client=None,
cached_lookup=None).tools(checker)` 返回四个案件工具：

* `collect_case_evidence`：当前案件绑定的原核算缺口 → 可选本机已消费 OrderFeed
  缓存 → 订单台现存订单及逐成员证据 → 按原始取价日、SKU、成本公司查历史价格。
* `request_member_seller_evidence`：只有已证明属于本案、并且缺失原单/成员依据的
  订单，才能进入现有 member seller evidence 请求队列。不是财务写入。
* `poll_member_seller_evidence`：保存队列状态、outbox sequence、台账 consumed sequence。
  已追平仍必须等待当前案件的新输入及现有核算，不能将排队成功标为案件成功。
* `apply_approved_history_costs`：仅向台账固定服务入口发送案件、原 run、coverage_key、
  context_sha 和 line_revision 等身份版本；不发送金额、证据 JSON 或任意 URL。
  后端独立重查固定历史来源，核对封存数量和单一组件，并验证人工已批准的店月规则。
  一个决定接受后等待重算和新案件，规则授权仍有效时继续处理下一笔，无需逐行确认。

所有工具仍返回 `(case, evidence) -> dict`；模型提供的 evidence 参数不参与身份或
查询条件选择。Worker Runtime 只将状态和 opaque evidence reference 送给模型。
原始订单、查询身份、响应和各层依据只留本机私有 JSON，绑定 input_sha 和 SHA256。

## 配置与复用

Config 中 `ledger_base_url`、`order_base_url`、`cost_base_url` 是可信配置的 origin，
路径由代码固定。每个服务有独立隐藏于 repr 的 token 字段；代码不搜索/读取 cookie、
session.json、历史脚本 secret 文件或浏览器登录态。构造 Collector 不发网络请求。
默认 HTTP client 不采用环境代理、不跟随重定向；403、登录 JSON、跳转登录页明确
保留为 login_required；超时、无原单、无配置、未确认归属都不会变成零金额。

既有 `ledger.order_feed.Client` 可直接作为 `order_client`，复用 get(path, params)
协议。已有 OrderFeed 实例可配
`cached_lookup=LedgerFeedCache(feed, {store_id: model_store})`，只调用
`alignment(store, key, live=False)`。不在取证工具内初始化、同步或改写 OrderFeed DB。
独立 Worker 默认使用固定 HTTP 合同，不要求安装 Polars 或整个后台包。

`member_requests_enabled=False` 可禁用队列 POST。GET 和 POST 先校验当前 lease、
input_sha；月看板还必须精确匹配 store_id、原 run_id。每次执行有请求/字节/缺口配额，
触顶明确 pending。队列写入前先持久 unknown_outcome 回执并加文件互斥；不确定结果
只能先查询/协调回执，不能盲目重放 POST。`Collector.close()` 关闭自建 HTTP client。

## 已核实的现有路径

* 台账：`GET /api/automation/months/{period}`、
  `/api/runs/{run_id}/pricing-gaps`、`/coverage-gaps`、`/api/order-feed/status`。
* 订单台集成：`GET /api/integration/ledger/v1/stores`、`/orders/{internal_id}`；
  请求使用 `X-Integration-Token`。缺内部 ID 时 `GET /api/orders?q=...` 仅作为候选
  搜索，必须再取 detail 并核对准确平台单/成员/店铺/SKU。
* 历史成本桥：`GET /api/cost/history?sku_ids=...&as_of=...&wms_co_ids=...`，
  请求使用 `X-ERP-Bridge-Cost-Token`。日期区间边界遵循已有源合同，结束日包含在内。
  `开始` 保留为显式无下界；未知边界、零/负/非有限价格不能自动补成已证实成本。
* 补采：`GET /api/integration/ledger/v1/member-seller-evidence/requests/{id}`、
  `POST /api/integration/ledger/v1/member-seller-evidence/requests`，
  POST 固定 `force=false`。上游受控 worker 负责 JST 只读调用和证据 outbox。
* 台账受控决定：`POST /api/automation/service-actions`，严格 DTO 为
  `request_id,rule_id,case_id,store_id,period,run_id,coverage_key,context_sha,
  expected_line_revision,input_sha`。rule_id 固定 `history_cost_single_component.v1`。
  后端源码/测试必须验证真实人工 grant 和独立原件，Collector 本地 found 不能授权。
  接受决定必须有 receipt_id/source_refs/recompute_required，不据此宣称月结完成。

以上路径经本地源代码核对，并以临时 HTTP 服务验证合同；没有声称生产已验证。

## 尚未具备的条件

* 没有已核实的通用历史订单补采/历史成本回写入口；此类请求只留隔离 proposal，
  pending_upstream_contract，不猜 URL、不改成本主档、不改人工核定/已结账。
  台账授权范围内的单组件成本补录走上述新服务决定，不回写 ERP 成本主档。
* 当前 ERP 订单或 `/api/orders/{id}/cost-history` 中的现存逐单成本，不等于业务
  发生时的价格。原取价日缺失或与成员证据冲突，保留 pending/conflict。
* 对重叠且价格不同的历史区间留全部候选，不由 Collector 选择一笔金额。
* 本机取证成功不等于证据已进入现有核算；需要既有来源/outbox 的真实回流和重算，
  最终完成只能由台账 Checker 判断。
* 没有生产登录/网络连通/真实缺口修复演练，也没有将合成契约测试计为历史覆盖率。

后端 `ledger.upstream_history.HistoryClient.from_env(ws.root)` 使用
`LEDGER_HISTORY_COST_API_BASE`（默认 `https://yongbo.cloud`）、
`LEDGER_HISTORY_COST_API_TOKEN`、`LEDGER_HISTORY_COST_API_TIMEOUT`，仅从可信服务环境
取配置。`quote(sku,company,pricing_day)` 返回 unit_cost/intervals/原件 SHA refs/quote_sha。
失败抛带 reason_code/status/artifact_refs 的 HistoryError，不吞异常、不补零。数量、
唯一身份、差额计算、人工规则有效期、cost_lines 审计及事务 outbox 由服务决定处理。
