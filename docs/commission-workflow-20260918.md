# 组织与实发修复验证

本地实现，尚未发布。

- 实发入口统一使用接口的 run_id。点击人员后保留人员范围，选择店铺月份后将该人置顶高亮。
- 保存仍是店铺月份全员核定，选择之前、表单顶部与保存确认均说明范围。核定不执行付款。
- confirmed_amount / actual_amount 仅含已核定金额，confirmation_state 区分 pending、partial、confirmed；原 amount 保留为核算或核定参考金额以兼容历史报表。业务导出显式区分参考金额与已核定实发。
- 组织及默认身份变更触发共享刷新；报表缓存键包含工作区 generation 与人员规则 revision。
- 负责店铺、默认身份、商品点数和上级抽成的保存与生效范围在界面说明。移除店铺先保留草稿，统一点击保存。

## 验证

后端：在 ledger 目录使用 `.venv/bin/pytest -q tests/test_commission_reports.py tests/test_workspace.py tests/test_api.py tests/test_org.py tests/test_store_member.py tests/test_gaps.py`。

前端：在 ledger/web 运行 `pnpm test`、`pnpm build`。

真实组件流程：运行 `pnpm dev`，打开 `/static/tests/payout-flow.html`。测试页面挂载真实 CommissionReports，拦截所有接口为隔离数据，自动点击人员、选择店铺、修改金额、填写依据、确认整店范围、检查保存内容。页面必须输出 PASS。此测试不接入真实业务数据库。

## 性能处理与边界

- A：仍保留一个 uvicorn worker。lifespan 每进程启动 NAS、订单 feed、重算和维护；Manager.start 会重置 running 任务。必须先实现后台单实例及故障接管，才可安全增加 worker。HTTP 同步处理已有线程池，report_query 已使用 run_in_threadpool。
- B：每线程 SQLite 连接启用 mmap 256MB、内存临时表；页缓存默认 16MB，可用 LEDGER_SQLITE_CACHE_KIB=65536 配成 64MB。页缓存按连接计，不是全服务共用 64MB。没有将该配置宣称为实测 30–50% 加速。
- C：总览和账期摘要使用持久化 overview_json，去掉 commission.products，保留检查与缺口所需数据；完整详情和结账仍读取原快照。
- D：保持 Decimal 金额运算，先批量读取店铺身份并索引原始人员；未将整个 build 重写为 Polars。合成样本为 57 店 × 12 月 × 35 人，684 个记录、23940 人员行，无商品明细。热查询三次中位数：HEAD 438ms，本地 315ms；参考金额和系统应发逐行相等。该结果不代表生产 18GB 数据库性能，也不覆盖全量复杂商品规则。
- E：启动预热覆盖所有历史月份当前展示的最新或冻结 run，按 16 条分批；被替代的审计 run 按需回填。生产检查发现 255684 个 run、891 个店铺月份，因此不在启动时预热全部旧版本。正常写入同步维护切片。数据库触发器在原结果更新时删除旧切片，读取不再用 length(大JSON) 判定。相同长度修改也会失效。旧切片首次升级需补 overview_json，会增加一次性 IO 与切片存储。

未执行多 worker 切换、全量 Polars 改写、生产性能压测或线上发布。
