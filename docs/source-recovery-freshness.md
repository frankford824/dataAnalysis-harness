# 上游补证与月结刷新合同

原件先由现有解析器读取，再保存只读 `source_recovery_originals` 侧索引。订单台在已声明查询窗口内替换旧成本时，旧价格继续退出金融输入；其原始文件 SHA、sheet、row 与内部订单身份仍可用于取证。后端独立重读 XLSX 和 ERP 原始正文，不采纳 Worker 提交的金额、数量、完成标志或任意路径。

`source_semantic(plan)` 签署原始平台/店铺/订单、原件行、原始日、店映射和解析合同，排除金融 run/input 与回执投递 nonce。新任务仍先检查当前金融 CAS；只有目标语义相同、旧原件/raw 独立重验、全部候选证明一致时才复用缓存。新财务尝试可重绑定同一原始证据，但旧租约和旧金融建议保持失效。

已采用 V3 成本事实使用 `verified_fact_source` 重新打开 server journal 绑定和原始 bytes。server `source_generation` 控制行明确选择当前取证代际，不能通过文件 mtime、首条或最新列表项猜测。即使计划相同，新 ERP 原文里的成员、数量或生命周期变化也会阻断旧事实；未完成刷新不能当作已经证明原事实错误。普通核算可显式 `allow_pending_refresh=True` 保留同语义、受控月结刷新前已经独立证明的阶段依据；结账复核使用默认严格模式，仍然阻断。

已认证人工月结预览调用 `ensure_close_refresh(period, store_ids, request_id, actor)`。同一请求封存店铺、事实集合及确认身份，首次强制创建持久新 capture，禁止缓存跳过；重复查询沿用同一代际。范围或事实改变必须重新预览。返回 processing/pending 时不得创建结账 proposal。完成后 proposal 绑定 `refresh_id`、`generation_sha`；confirm 调用 `verify_close_refresh` 并继续执行原金融 CAS、前缀 H、冻结与业务授权检查。后续新代际会使旧 token 失效，旧 Worker 不得回写新代际。

日期事实的相关性来自原始退款输入的潜在订单 ID，包括未关联、未计入和日期未知行。日期事实同事务写入后，其内容摘要立即进入相关店月的金融 input SHA；无关店/PID 不误伤，日期本身未变的重复回执不引入金额变化。案件月份是取证来源，不是原订单日期的永久使用范围。

这些合同不等于真实业务验收或上线收据。合成 raw/XLSX 反例只验证边界；真实输入、金额预期、影子周期和生产发布门禁仍分别保留。

`proof_semantic` 分别保存金融语义和原始引用。HTML 非经济元数据、capture SHA、原始数组位置及数量尾零不构成经济变化；原件锚点、身份、SKU、单位、数量、原日、公司和全部已观察的生命周期/风险标志仍参与比较。完整原件与每次新 capture 的 SHA、receipt 和引用继续独立核验、保护，不能为减少误失效而丢掉血缘。

技术失败恢复在服务端队列完成，不进入模型等待：同一 Worker heartbeat 每次最多重排 1 个任务，定时延迟为 60/300/900/3600 秒，最多 4 次；每 Worker 的 UTC 日自动定时重排最多 16 次。到期前不发新 GET；达到日上限延至下一 UTC 日，达到单任务上限则保留 maintenance 及最后失败原因。状态接口返回 `retry` 的原因、计数、状态和下一次时间。每次重排重新核验当前金融 CAS、明确执行范围、publication 身份、代际和冻结状态，并递增租约令牌，旧 Worker 不能完成新尝试；旧回执及失败原因继续保留。

只有明确 transport/暂时 HTTP 5xx、408、429 等及本机暂不可用进入定时重试。固定 reader 可能把登录页折叠为 transport，因此后端重新验证回执中实际响应 SHA/status/登录标志：200 登录页、401/403 不定时重查，等待真实 session/config 内容摘要改变后立即有界重启同任务。缺原件、数量或身份/关系语义冲突、字节预算和未获资格等不因时间流逝重复 GET。session/config 实变与定时重试分别记账，均不使模型回复或旧金融建议成为完成依据。

纯月结刷新只有在 server 控制行、当前代际、真实当前 run/proof 的 used 事实与新独立物理证明语义全部相符时，才不投递金融重算。新 capture 的原件、raw、receipt 和独立 proof SHA 另存 `source_capture_observation`，并由月结代际收据引用；不改旧 run/proof 以补血缘。物理证明变化继续待核并投递正常重算，不能靠刷新把变化隐藏。

原订单日期另行版本化：同 store/PID/original_day 复用已存在的 `date_source_fact`，把新 raw/receipt 引用追加为 `date_source_observation`，不改日期指纹、不投递重算。真实日期修正写新 immutable fact、明确的 server `date_source_head` 与同事务 outbox，旧 fact 保留；后续读取选择该受控 head，而不是按文件 mtime 或任意最后一条挑日期。日期指纹在写入后立即改变，仍由原财务 scope/CAS/冻结保护决定哪些开放账期可发布。现存存储维护器不清理 source-recovery/ 技术目录；以后若扩展清理，必须保护两类观察的 source_refs 以及月结代际引用，不能只保护旧金融 run 的原始引用。
