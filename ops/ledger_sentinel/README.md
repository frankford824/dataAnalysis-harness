# Ledger 运维哨兵

该模块只读检查服务版本、Manager积压、feed水位、索引、磁盘、SQLite、备份、JST会话和Worker心跳。未知、缺字段、过期及连接失败均不会当作健康。SQLite保存事件、站内通知、租约与带校验值的执行回执；相同事件持续失败不会反复通知，新一轮故障会取得新的事件编号。

恢复与发布只运行配置中的固定argv，`shell=False`，不支持内联命令。Windows子进程隐藏窗口，超时终止本次进程树；所有维护动作共享租约，幂等键不允许换配置重用。前置条件或备份不成立时不发布，读回不成立时执行固定回滚并验证旧状态；回滚失败产生升级通知。脚本输出只保存SHA，避免凭据出现在通知中。

发布必须消费 `ledger.release_gate.validate` 接受的同一收据，重新校验真实输入、规则和前端产物，验证CAS旧版本与完整备份后才运行部署。成功读回后才发布新的控制指针。脚本通过 `LEDGER_CANDIDATE_CODE_SHA`、`LEDGER_PREVIOUS_CODE_SHA`、`LEDGER_ACCEPTANCE_RECEIPT_SHA`、`LEDGER_BACKUP_MANIFEST` 获取已经验证的参数，不接受任意shell或财务操作。`current_state` 应是执行器专用的发布控制指针，部署脚本不应修改它。

`sentinel_api.install(app, home, max_age_seconds=180)` 只提供事件、站内通知和状态GET，未运行和过期为unknown，不启动恢复。Manager探针可配置 `worker_probe` 关联独立心跳；Worker支持 `timestamp_field: last_seen`，若使用Unix秒还需显式 `timestamp_unit: epoch_seconds`。JST探针只读独立的登录健康凭据，模型与通知不接收cookie；没有当前探活和到期凭据时保持unknown。

运行环境需要安装项目的 `ledger` 包并让仓库根目录位于 `PYTHONPATH`：

```text
python -m ops.ledger_sentinel --config sentinel.json --home /isolated/state check
python -m ops.ledger_sentinel --config sentinel.json --home /isolated/state monitor --interval 30
python -m ops.ledger_sentinel --config sentinel.json --home /isolated/state execute release --request-id UNIQUE --receipt acceptance-receipt.json --candidate-version FULL40SHA --expected-current FULL40SHA
python -m ops.ledger_sentinel.drill --output /new/isolated/fault-drill
```

配置格式为 `schema_version: 1`，`probes` 和 `actions` 数组，每项必须有独立 `id`。探针kind支持 `http_json`、`json`、`manager`、`feed`、`worker`、`jst_session`、`disk`、`sqlite`、`backup`。HTTP只GET，可通过 `header_env` 引用机器环境变量而不持久化其值。JSON断言需显式field/operator/value；部署后版本断言可使用 `$candidate_version` 和 `$candidate_source_code_sha`，回滚版本用 `$current_version`；候选收据缺源码SHA时该探针为unknown，不以null相等放行。读取和恢复脚本的实际配置与生产地址不随本模块默认启用。

发布action需要 `kind: deployment`、`capability: technical`，及 `preconditions`、`postconditions`、`rollback_conditions` 探针ID列表。`backup`、`deploy`、`rollback` 每项具有绝对可执行文件argv和最多3600秒timeout_seconds。还需配置 `backup_manifest`；financial deployment强制workspace/registry/application/model四类备份，配置required_backup_roles只能增加要求。另需`current_state`、`model_root`、`corpus_root`、`frontend_root`、`evidence_root`。备份清单应由备份脚本原子发布，具有带时区completed_at、旧code_sha和各role的路径/SHA。只有全部必需角色存在、校验通过且是本次操作产生的备份才发布。Executor调用受保护的tools/ledger-backup.py verify，实际读SQLite、归档及冻结层并校验restore proof，四个文本文件或手填passed不能通过。recovery不强加财务四角色。

M6命令为 `capture-observation sample.json`、`record-observation cycle.json`、`readiness shadow|canary --code-sha ... --model-sha ...` 和 `promote shadow|canary|production --action-id release --receipt ... --version ...`。只开启开关不增加观察数。升canary要求同一代码、实际源码SHA、规则及验收收据已有一个完整shadow结账周期；最终production要求至少两个相邻业务账期的实际观察，末期包含canary。因此可以第一期shadow、第二期canary，不额外增加到四个月。每期默认至少86400秒及两次独立源事件的采样，七项检查全部明确通过且金额差异为零。

`sample.json`使用schema 2，显式标注 `measurement_kind: actual|synthetic`，包含phase/period/code_sha/source_code_sha/model_sha/input_sha/acceptance_receipt_sha，以及七项checks（name/artifact/sha256）。每个artifact必须是结构化JSON，内部同样绑定上述身份、name、measurement_kind、带时区observed_at和不可重复的source_observation_id；result包含status=passed、正整数checked以及整数failures/errors/skipped/unknown全部为0，金额守恒另须difference精确为0。采样只接受120秒内原件，Sentinel以自身时钟追加不可修改的sample日志。调用者填写started_at/finished_at/sample_count不参与计时；同一输入或源事件不能换period标签再计一期。

真实采样还须action_id、acceptance_receipt路径及semantic_model_sha；执行器重新验证同一可信发布收据及配置model_root，采样身份必须匹配已经实际启用的阶段。`cycle.json`仅提供 `{"schema":2,"sample_ids":["FIRST_ID","LAST_ID"]}`，起止和次数从已留档采样推导。readiness配置中的 `workspace_root` 是受信本机台账目录；最后一次采样须有close_receipt_id。record只读核验现有close.confirm收据、human授权、原预览、完整批准营业范围、当前closed运行、封存proof及原件SHA，且预览、授权、结账必须在本轮采样窗口内。它不执行月结、不代签；旧闭账回放及手填manual_close不能形成周期。 OrderFeed内容SHA是派生frame身份，不被假设为上传CAS文件。含这类来源的实际周期还须配置只读input_bundle_root，并在最后样本提供input_evidence={ref,fingerprint}，指向既有冻结输入格式；核验完整H0对象/增量SHA、model/period、原slots和closed run的snapshot/seq对应。缺原始输入包保持blocked，不能用投影或旧运行金额替代原件。合成元数据只能留档和显式模拟评估，永不用于promotion；改动身份、证据、账期或反结账后，原观察失效。

本模块未连接生产、未安装计划任务、未执行真实服务恢复或发布，尚无两个周期的生产观察。财务政策、反结账、历史金额修改、实发核定继续由既有业务授权路径处理。

本次上线另受 2026-10-27 已确认范围约束：只自动处理明确批准的新账期，历史分摊仅保全与列清单，不执行历史差额补扣、补发、结转或积压自动入账。历史只读回放不计实际观察周期；新账期范围不能凭服务器日期自动猜定。详见 [月度自动化执行合同](../../docs/automation-contract.md#本次上线范围只自动处理新账期)。

## AgentScope 隔离修复适配

`repair.Repair(repo, home, config)` 的home必须在原checkout之外。`prepare(event, full_base_sha)`生成脱敏事件、独立detached worktree和原checkout身份。原来的已跟踪与未跟踪用户改动不会被覆盖。事件不含原始日志、业务行、会话或真实语料；模型初始上下文只能读取生成的`event.json`与被授权的代码。

root AgentScope运维Toolkit应**仅注册** `repair.model_tools(repair_id)` 返回的list_code/read_code/write_code/run_check。不存在任意host shell工具；run_check只接受配置的check_id。代码路径有数量、大小、后缀与prefix限制，禁止越界、符号链接、数据/秘密目录、财务model、旧baseline和门禁/执行器控制文件。不能把工作区根目录或真实corpus注册为模型文件工具。

权威代码还包括所有automation模块、认证/Worker控制、Workspace结账/finance_guard、提成/成本/人工决定、可信历史来源与封存/复用，以及agent-worker/host-agent/ops/tools。`../`、`./`或大小写变体不能绕过保护。prepare从base commit留存所有既有验收文件；旧测试只能读，不能改预期、删除或改名。新反例只能是新的`ledger/tests/regressions/test_*.py`，不能添加conftest或包初始化钩子。

配置的check具有固定容器内argv、timeout_seconds与JUnit相对report路径。执行仅采用bwrap `--unshare-net --clearenv`或SHA固定的Docker image `--network none`；没有后端、后端启动失败或没有可验证的测试report时明确blocked，绝不降级host执行。模型阶段只挂载获准代码的独立投影以及固定argv明确指定的测试文件，不挂载整个checkout、.git、models、baseline或secret目录。run_check只返回reported_pass/reported_failure与authoritative=false；候选进程可控制的JUnit统计、失败名和输出hash都不回传模型，不能当作修复证明。可选sanitized_fixture_root需要`sanitized-fixture.json`声明kind=synthetic_reproduction、business_rows=false及文件SHA；它不能指向受保护的真实语料/私有日志。仅复制manifest白名单文件到新的fixture投影，原目录及未列出的.env/日志不挂载。

`freeze_patch()`在独立worktree校验、保存proposal.patch并生成本地candidate commit，不改原checkout、不push。模型编辑随后关闭。`accept()`不运行旧的trusted=True sandbox；该路径已禁用，避免在独立校验前先向候选暴露corpus/expected。它仅调用host注入的独立oracle，真实财务材料、期望和裁决输出由该oracle隔离管理，完整证据保留在模型不可访问的authority目录。candidate可写的/out永远只是私有诊断，不能签发财务验收。Repair构造器的oracle参数只能由可信host注入独立校验器；JSON配置中的布尔或路径不能启用该权威。未配置时在暴露corpus之前blocked（independent_financial_oracle_unconfigured）。独立校验器应隔离候选执行、将期望与裁决器保存在候选不可读写处，返回independent_financial_acceptance.v1合同，并在host生成、从未挂载给候选的oracle-authority目录写入同candidate release receipt。控制器再用ledger.release_gate.validate核验；直接复制候选/out或自报code_verified均不构成独立权威。独立 financial oracle 的实现与隔离合成回归已提供；当前尚缺批准的真实案例材料及生产权威配置，因此不能自动把 Agent 修复发布到生产。

冻结后模型read_code也关闭，避免不可信候选在可信执行时把原始语料写进代码，再经读取工具回传。新增Python反例可能含导入副作用，不能直接导入官方gate进程。外部oracle提供同一验收收据时，必须附repair_regressions：code_sha/files匹配冻结候选，excluded_from_official_gates=true，isolation=separate_pytest_process，且独立报告无失败/错误/跳过。该报告也必须由外部authority管理；复制候选JUnit不构成独立验证。

外部实现见financial_oracle.py/financial_oracle_child.py，CLI通过configured_financial_oracle(config)接入，缺配置返回None。financial_oracle配置绑定批准manifest及SHA、实际runtime、implementation_sha和其余可信release证据；fixture和缺材料不会签发生产收据。候选只获得代码投影、去除expected的bundle/seed/steps和临时Workspace，父进程独立读取SQLite、封存物、金额、确认、scope及冻结状态。runtime_mounts仅用于模型阶段依赖环境，不得覆盖代码/数据目录；Docker必须配置name@sha256:...。

`artifacts()`输出impact.json、proposal.patch、pr-body.md和建议的`codex/ops-repair-...`分支。若配置绝对gh路径，会生成draft PR的固定`gh pr create --draft --body-file ...`参数；不会联网建PR、push或merge。远端身份尚未确定时保留本地可审查产物。CLI为 `python -m ops.ledger_sentinel.repair --repo ... --home ... --config ... prepare|check|freeze|accept|artifacts`。

当前测试在临时Git仓库验证工作树/WIP保留、脱敏、路径保护、无隔离时blocked、断网命令与真实门禁收据适配。已在本机实际bwrap断网沙箱执行合成缺陷闭环：既有测试先失败、修改旧测试被拒、仅修源码后通过、冻结后模型不能再读写，并用沙箱外纯函数AST oracle独立检查4个合成输入；没有独立financial oracle继续阻断。另用固定受信源码执行真实case预检，缺批准材料继续阻断，此预检不执行候选代码。入口为 `python -m ops.ledger_sentinel.repair_drill --output /new/private/drill --repo /trusted/repo --runtime /trusted/ledger/.venv --model-root /trusted/model --corpus-root /trusted/corpus --target-period YYYY-MM`。该演练不代表LLM评测、财务真实验收或生产周期；SDK独立ops标定由agent-worker的评测工具负责，不能复用业务证书。实际可信corpus修复及远端PR尚未完成，红灯继续阻断发布。


独立财务 oracle 的执行边界只传已批准的 `publication_scopes` 店月键、业务源输入及操作步骤，不传 parent 的 expected 金额、批准 manifest 或发布 authority。child 的 Session 必须带明确 scope，不能默认全月份；每个检查点不得出现批准范围以外的店月。所有 `automation_run_proof.manifest.artifacts` 原件及来源证据按安全相对路径和 SHA 导出，parent 再独立核对，不能把缺原件从断言中删掉。实际 child stderr 保存在 authority 私有 `child.private.log`，不只留下不可诊断的 hash。

当前 oracle 运输协议支持 `recompute`、`supplement`、`confirm`、`replace_scheme`、`drain_pending`、`close_approved`、`restore_approved_manual_finance` 七类步骤。整期人工恢复必须绑定已封存 bundle 内的原始 capture SHA、精确 decision 字符串 SHA、历史 run、原 result SHA、决定时间及本次 case 的批准引用；原 result SHA 不冒充原件 input 身份。父进程校验来源，子进程仅接收只读原始业务输入路径，不能覆盖 fixture 类型或获得 expected。该步骤调用真实人工预览及月结入口，仍受公司收入、公摊、输入版本和冻结检查，不能用历史决定绕过当前门槛。`manual_finance` 与已结结果、公摊及实发同样纳入跨步骤不可变核对。

真实 `historical_golden` 的销售收入、毛利、利润必须明确 `available=true` 且为有限数字；未知、布尔、数字字符串、NaN/inf 不能冒充正向财务覆盖。合成 complete 输入、缺价 pending 输入、源身份替换和篡改人工决定均保留独立隔离测试，任何合成通过都不生成生产收据。真实 PDD 2026-06 原件/人工确认整链已实际运行：先修复冻结映射 DTO 缺少 SQLite payload 的适配失败，再进入真实重算和人工关闭入口；公司收入、来源解析/身份等阻断仍保留，尚无完整真实正向通过或上线收据，不得据七步运输已打通宣称 M5 完成。Ops executable 协议改变后旧 Ops 100 次 profile 立即过期，需冻结新字节后重新运行真实配置合成评估；业务/source SDK 执行文件不因此重写。
