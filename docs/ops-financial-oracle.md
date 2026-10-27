财务外部验证器使用现有 `historical_golden` service-case manifest、原始 bundle、固定人员规则和批准步骤。它不生成预期值，不将候选计算结果录为基线，不授予月结或实发权限。缺少完整材料时返回 `blocked`。

父进程保留 manifest、expected 和收据输出目录。断网 bwrap 子进程只收到 Python 包投影、原 bundle、seed、批准步骤及空结果卷；不挂载原仓库、测试基线、原 Git 历史、预期值或权威输出目录。投影中父进程新建的临时 Git 仅包含本次 Python 包字节，用于现有开发源码护栏，不代表发行批准。

子进程运行真实 `replay_service.Session` 和 service/Manager/决定链，并在每个步骤后导出实际 SQLite 及封存制品。父进程通过只读 SQLite 连接检查完整店月范围、原始 run proof、封存文件 SHA、金额及人员、核定合计守恒、已结记录和已确认实发的保全。严格 RPC 中的就绪状态与原始 SQLite 金额交叉核对，再与父侧批准预期比较。子进程退出码、JUnit、`/out`、自报通过均不构成验收凭据。

`configured_financial_oracle(config)` 接受固定服务配置：

```json
{"financial_oracle":{"kind":"external_service_cases.v1","manifest":"/evidence/approved-service-cases.json","manifest_sha256":"<批准文件SHA>","runtime":"/runtime/ledger-venv","implementation_sha256":"<implementation_sha()>","timeout_seconds":600}}
```

`implementation_sha()` 绑定父比较器、执行 transport、现有 service-case 预检器和可信 ledger Python 源。配置不来自模型工具参数。接口 `verify(...)` 生成私有 `financial-oracle.json`、父侧 JUnit 和子工作区副本；失败输出仅原因码与哈希，不回传原始业务行或日志。

这项证明只覆盖财务服务链。`Repair` 的 host authority 只有在另行配置固定 SHA 的可信完整发布收据、复核同一 candidate/source/model/input/approved-case 身份和全部既有门禁后，才组合签发 `acceptance-receipt.json`。没有其他发布门禁时，财务验证成功仍返回 `independent_release_checks_missing`。测试夹具通过永远不会签发行收据。

本机测试包括真实合成原件的两店两月 Session、缺成本保持利润不可用、实际成本补录和核定后月结再重算、关闭记录篡改、退出零码、伪造输出、宿主符号链接读取以及 `/proc`/文件系统无法读取父预期的反例；这些夹具不是已批准历史业务案例。
