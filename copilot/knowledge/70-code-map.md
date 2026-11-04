# 代码地图（生产路径 D:\ledger\app；开发仓库同结构）

## 服务与启动
- 台账：uvicorn `ledger.api:app --workers 1`，0.0.0.0:8000。启动脚本 tools\deploy\win\serve.ps1（计划任务 LedgerHarness，开机启动，崩溃自动重启）。PYTHONPATH=D:\ledger\app\ledger，python=D:\ledger\venv\Scripts\python.exe。
- 关键环境变量：LEDGER_HOME=D:\ledger\home、LEDGER_INDEXER_URL=http://127.0.0.1:8765、LEDGER_ORDER_FEED_URL（订单台 8001）、AUTO_RECOMPUTE=1、LEDGER_RECOMPUTE_LIMIT=2、POLARS_MAX_THREADS=4、LEDGER_LAN_OPEN=1（局域网免登录）、存在 D:\ledger\nas.enabled 时用 NAS 模式。
- 索引器：计划任务 LedgerIndexer（8765）。助手：计划任务 LedgerAgent → agent.ps1 → `python -m ledger_copilot.server`（8020）。
- 停台账：stop.ps1 会结束占用 8000 的进程。助手改完台账代码后用 restart_ledger 工具（自动比对最近 3 个已结账月金额，变了自动回滚）；改前端用 build_web。

## 台账后端 ledger/ledger/
- api.py：主路由（导航、trend、店月详情、gaps、drill、search、结账/反结账、重算、费项、上传、index）。trend 在 `_trend_data`/`_trend_rows`。
- operations_api.py + operations_readmodel.py：/api/operations/month（看板口径公式在 `derive()`，NODES 映射损益节点）；operations_income.py 收入证据。
- automation_api.py：/api/automation/*（months、inbox、rules、decisions、close-batches、questions、agent-authority）；automation_actions.py 人工确认动作；automation_checker.py 月度就绪与结账检查（close_verification 的 code 与中文消息在这里）。
- commission_api.py：/api/commission-v2/*；commission_engine.py 逐单分配；commission_registry.py registry.db 表结构；commission_reports.py 金额汇总；commission_profit.py 人员利润构成；commission_confirm.py 实发确认；labor_api.py 兼职。
- freight_review_api.py、freight_trial*.py、freight_shipments.py：运费复核与试算。
- service.py：编排（交表→留档→自动算账→存快照）。workspace.py：workspace.db 表结构与读写。view.py：结果→界面结构。gaps.py：店月缺口清单。
- order_feed.py：订单台 → 台账（成本、售后、is_suspect、缺价）。nas_ingest.py：NAS 收件。search.py：全文检索。
- fees.py：费项目录；cost_lines.py 逐单补录成本；manual_cost.py 整期成本确认；brushing_zero.py 刷单零成本；statement_review.py 冲突流水人工选择；frozen_refund_guard.py 已结退款防重复；historical_close.py 历史定稿导入。
- cli.py：`python -m ledger.cli stores | store set | store add`（店铺登记）。
- static/：前端构建产物（勿手改）。

## 引擎 ledger/ledger/engine/（Polars，确定性）
recognize.py 识别表类型 → parse.py 解析行 → normalize.py → link.py 按键挂单 → classify.py 按字典归类 → calculate.py 按公式树求值。其他：allocation.py 分摊（缺数不当 0）、statement_dedupe.py 流水去重、cost_policy.py 代发识别、promotion_allocation.py 推广跨店/托管分摊、refund_dates.py 退款归月、order_identity.py 订单号归一、controls.py。

## 模型 models/cn-ecommerce/
statement.yaml（损益表节点与公式）、metrics.yaml（每个指标取哪个源、哪些列、排除条件，如售后成本排除）、checks.yaml（结账检查与门槛）、sources.yaml（资料清单、必需/可选、去重键）、templates/（各平台表头识别）、dictionary.csv（科目→指标）、stores.yaml（店铺登记）、overheads.csv（历史兼职）、commission.csv（历史提成规则）。改模型会触发重算并影响金额 → 必须走确认卡片。

## 前端 ledger/web/src/
router.js 路由；App.vue 布局；components/AppBody.vue 左侧导航；views/ 各页（BoardView、DeliverView、PeriodView、FreightReview、FeesView、LaborView、OpsView、Commission*）；api.js 接口封装（按函数名搜就能找到对应 URL）；components/AgentPanel.vue + agentClient.js 右侧助手。

## 助手 copilot/ledger_copilot/
- config.py：路径、端口 8020、模型（quick qwen3.8-flash / investigate qwen3.8-max / engineer kimi-k2.7-code / vision qwen3-vl-plus）、月预算 2000、MAX_STEPS 16。
- agent.py：系统提示（含本目录 00-index.md）、模型路由、记忆压缩。
- tools.py：工具实现。ledger_get（90s，输出截 6000 字）、ledger_preview（只允许 *preview*）、api_index（查 openapi）、sql_query（只读，60s，200 行）、read_file/search_files/write_file/edit_file（写前自动备份）、powershell、python（预置 get()/sql()）、restart_ledger、build_web、read_knowledge（本目录，按文件名关键词）、remember/record_correction/search_memory、auto_close_status/run_auto_close、propose_change→execute_card。
- autoclose.py：夜间自动结账；server.py/webui.py：HTTP 与 SSE；store.py：话题、记忆、纠错记录。
- knowledge/：本系统地图（00-index 常驻加载，其余按需读）。

## 排查顺序建议
接口返回异常 → 看 serve 日志 Traceback → 定位 api.py/对应 *_api.py 路由函数 → 业务函数。金额口径问题先查 models/cn-ecommerce 的 yaml，再看 engine。
