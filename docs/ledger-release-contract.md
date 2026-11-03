# 台账发布与恢复合同

这套入口只发布已经通过完整验收的提交，不会在发布时重录财务基线、复制开发机密钥、全量覆盖生产模型或清理旧业务数据。开发检查点和上线收据是两种不同产物。

## 同一份验收收据

`tools/verify-ledger-acceptance.py` 运行后台、真实语料、实际服务链案例、不变量、浏览器、前端构建、SDK Worker 与运维检查。代码、真实输入、实际运行模型、前端产物和检查报告共同绑定。需要显式账期；缺少已核准的真实服务链案例时生成失败记录，不签发合格收据。

```bash
ledger/.venv/bin/python tools/verify-ledger-acceptance.py \
  --corpus /controlled/real-corpus --runtime-model /controlled/runtime-model \
  --service-cases /controlled/approved-service-cases.json --target-period 2026-08 \
  --output /controlled/new-release-evidence
```

### 没有引擎基准时的业务负责人声明

2026-11-02 生产核查：2026-06 的 42 个、2026-07 的 43 个已结账店月全部经人工结账（`manual_finance`，且均忽略过检查项），更早月份为旧版留档结账，生产历史中没有“引擎算出、财务直接认可”的店月可作服务链案例。业务负责人当日决定以声明代替服务链案例：

1. 在生产机上只读运行 `tools/build-historical-golden.py`，得到 `selection.json`；
2. 业务负责人签署 `kind: no_engine_baseline_attestation` 的声明，`evidence` 以 SHA 绑定该 `selection.json`；
3. `verify-ledger-acceptance.py --baseline-attestation 声明.json`（与 `--service-cases` 二选一）以 `baseline_attestation` 检查代替 `service_replay`，其余检查不变。

`tools/verify-baseline-attestation.py` 只在报告未选出任何店月、且目标月与上月的每个店月都因 `manual_finance` 或 `not_closed` 被排除时通过；只要存在可重放的店月就失败，必须改用已核准的服务链案例。收据记录 `baseline_attestation`（声明 SHA、批准引用），声明和证据文件同属收据输入清单。上线后由生产机的升级核对把关：新旧版本结果有变化的店月，人确认前规则执行器不自动改账；结账、实发仍由人确认。

旧语料红灯不能通过放宽金额阈值、跳过、把未知补零或重新录基线消除。财务核准后的输入修复和案例预期变更需要保留证据及原因。

## 生产版本漂移

本轮发现生产 `VERSION` 对应提交之外还存在源文件修改。因此发布前清单要记录实际部署文件 SHA，不能只用旧 Git 提交推定生产文件。打包器将实际基线与候选提交逐文件比较；发布执行器在换代码前再次核对完整实际基线，包括本次没有覆盖的源文件。未知模块删除会阻断，不能静默丢弃线上修复。

实际运行的业务模型可以与仓库示例模型不同。收据绑定本次真正执行的受控模型快照；普通代码发布保留生产模型。允许更新的模板、来源及指标 YAML 也必须正好是验收执行过的内容。

完整后台 Python 源码摘要同时写入收据和发行目录的 `RELEASE-ACCEPTANCE.json`。运行时检查实际源码与启动时固定的批准收据；文件漂移、收据丢失或被改写会暂停写入、核算发布及结账。`/api/version` 同时提供实际源码摘要和批准状态，版本标签相同不能掩盖文件变化。源码已恢复也须经过正确重启和发布读回，不能由运行中的模型自报恢复健康。

## 独立备份及恢复检查

`tools/ledger-backup.py create` 在新的隔离目录中，使用 SQLite backup API 生成 Workspace 与 Registry 副本，包含已提交的 WAL 内容；不能只复制主 `.db` 文件。随后对副本做 `quick_check`，比对受保护财务记录及结账结果的逻辑摘要，并逐文件校验代码、模型归档。源头核定记录在备份期间变化则失败，不产生成功 manifest。

```text
python tools/ledger-backup.py create --version DEPLOYED_VERSION \
  --root WORKSPACE --application APP_ROOT --model RUNTIME_MODEL --output NEW_BACKUP_DIRECTORY
python tools/ledger-backup.py verify --version DEPLOYED_VERSION \
  --manifest BACKUP_DIRECTORY/manifest.json --compare-root WORKSPACE
```

成功 manifest 必须包含 application、model、workspace、registry 四类及匹配的恢复检查证明。校验会重新读取副本和归档；文件存在、复制成功和“备份脚本退出 0”不能代替恢复检查。它不自动恢复生产，不删除部分失败备份，也不宣称 Workspace/Registry 的分库在线备份是跨库同一事务。受保护财务层必须一致；原始材料和 NAS 的灾难恢复仍属于各自的原件备份与保留策略。

显式允许额外角色时，额外制品只做路径与 SHA 验证，`restore_verified_roles` 仍仅列出上述四核心恢复角色；不能把一个原件备份文件的存在称作它也完成了恢复测试。

备份目的目录不能位于应用、规则或工作区内部，权限限当前执行身份。释放空间或替换生产数据库需要另一套明确的迁移执行流程，不能拿此工具执行。生产大库的备份时间及磁盘占用须实际测量，不能以合成小库测试推断。

## 唯一常规发布入口

`tools/deploy/ship.sh` 现在调用 `ship-ledger-release.py`。默认只打包，只有显式 `--deploy` 才连接已配置的 SSH 主机。旧全目录解包和旧 UI 单独发布入口已阻断，避免绕开同一份验收。

```bash
tools/deploy/ship.sh --base PREVIOUS_GIT_SHA \
  --receipt /controlled/new-release-evidence/acceptance-receipt.json \
  --base-manifest /controlled/actual-production-source-manifest.json \
  --output /controlled/new-release.zip
```

实际发布还需 `--deploy --host finance-win --expected-version DEPLOYED_VERSION --period YYYY-MM --user-base-url USER_ENTRY --backup-manifest D:/ledger/backups/ID/manifest.json`。无合格收据时不会到达 SSH。ZIP 和提交内的 PowerShell 执行器都进行传输 SHA 校验。

Windows 执行器核对备份与当前冻结财务记录，取得维护锁，停止属于台账的所有 API 写进程，捕获停止后的冻结快照，再次检查代码基线和备份覆盖，随后替换代码。启动后需要实际用户入口版本、公司范围、原始凭据支持的收入、派生公式及历史冻结全部通过。

失败会回滚代码并先在停止状态验证原财务层。代码回滚不能修复财务数据差异；历史校验仍不通过时保持维护状态并产生可诊断失败，不能重新启动旧版本假装恢复成功。

## 运维修复的受信门禁交接

运维修复不接受候选 `/out` 的自签报告。已有受控 CI 完成全部门禁后，可触发 `tools/accept-ledger-ops-receipt.py`：它只读取固定私有 `receipts-root/<candidate SHA>/acceptance-receipt.json`，重验真实报告、输入与固定门禁入口 SHA，自动计算本次收据 SHA 并交独立金融父验证器。维护者只在初始配置批准入口及证据目录，不必每个候选手填收据哈希。

该交接工具不执行候选、不运行模型、不发布，也不使任意报告变得可信。证据目录必须从未向候选开放写权限；受控 CI 仍须按既定隔离与源码策略执行。本次无真实案例/完整资格收据时会继续阻断，不能用它绕开门禁。

## 已验证的边界

本轮合成真实数据库测试覆盖 WAL 备份、核定记录保护、归档篡改、重封数据库、并发财务变更和无收据不得连接网络。Windows 脚本做原生语法及进程归属判定检查。以上不等于生产大库恢复演练、真实发布成功或两个结账周期已经观察完成。
