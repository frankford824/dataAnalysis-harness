# 独立 ops 标定与修复边界

业务 `evidence` 证书不能批准 `ops`。核心 `runtime.py/privacy.py/state.py` 和 evidence SKILL 的身份保持不变；ops 另绑定运行时、oracle、评估器、CLI、ops SKILL 与 Sentinel Repair/执行器/状态/探针源码。

## 实际标定

从仓库根目录使用既有台账网关配置，密钥仅由 `ledger.assist.load_config()` 在本机读取：

```sh
PYTHONPATH=agent-worker:.:ledger agent-worker/.venv/bin/python -m ledger_agent.ops_evaluate \
  --output /srv/example/sdk-ops-evaluation-unique \
  --runtime /home/example/dataAnalysis/ledger/.venv \
  --model deepseek-v4-flash-0731 --rounds 5 --concurrency 2
```

输出目录必须是仓库外的新目录。完整评估包含二十个不同的合成纯函数故障，每案五轮。子集或单轮可调试，但不生成可用 profile；实际会调用模型并记录供应商返回的 usage。未配置价格时费用保持未知，供应商账单单独核销，不能把 token 统计称作账单。

每轮先由父进程控制的 oracle 在真正断网 bwrap 中确认原始程序不符合独立预期，再让模型使用 `list_code/read_code/write_code/run_check`。补丁冻结后模型工具关闭；候选仅得到单个只读纯代码文件和输入，拿不到期望、权威输出目录、整个仓库或真实资料。父进程比较结果、自行写出收据；候选 JUnit 和退出码只作反馈，不能自行证明修复成功。

合成 oracle 使用明确的纯代码能力范围、资源上限与有界输出，并禁止文件、网络、进程和帧反射。指定纯操作包括 math、collections、itertools、functools、statistics、calendar；字面量 format 只允许简单字段，不允许属性、下标或嵌套遍历。未支持语法按 policy/harness 阻断记录，不能笼统称为模型修复错误；所有数值比较保持原测试的精确语义。此能力范围不冒充任意 Python 程序的通用安全证明。原有测试 SHA、原仓库状态、冻结源码 SHA、外部 oracle 收据、每轮真实模型调用审计和完整重复维度都必须保持一致。

`ops-evaluation.json` 保存所有结果；只有完整真实 provider 评估、所有正常案例经外部 oracle 通过、无错误完成、源码身份未改变时，才生成 `ops-model-profile.json`。全 blocked 不算正确。审计与 trial 证明目录须同报告保留，不能只拷贝一份自声明摘要。

## 准入与运行

生产 `run_repair` 先通过通用 `Config.select(('ops',))`，再通过 `validate_ops_model(config)` 验独立 ops 身份与实际证明。CLI 在创建工作树和读取修复事件之前进行同样验证。校准配置不能授权生产修复，给 evidence 报告改 skills 也不能过关。

纯函数合成 oracle 仅能批准其声明的模块与函数合同。普通维护事件缺少候选进程外的独立 oracle 时，运行时最多生成 `patch_unverified_release_blocked` 候选，不把候选写出的测试报告计作 `code_verified`。

## 代码验证与发布分开

代码通过外部检查不等于真实财务验收通过。真实语料只能在补丁冻结、模型完全结束后由可信阶段读取；无独立财务 authority、缺少批准的真实基准或财务检查失败，发布继续 blocked。模型不能修改政策、控制面、原测试或验收规则，不能靠文字宣布上线。当前标定只验证合成代码修复，不声称真实财务缺口已经修好；实际财务门禁另有本机材料和收据。

## 2026-10-27 本机资格结果

最终批次 `sdk-ops-live-100-02` 已实际完成二十案、每案五轮：100/100 经独立父进程 oracle 通过，错误完成 0；新进程的严格准入复验通过。报告、profile、逐轮证明和私有调用审计保存在仓库外 `/home/wsfwk/.ledger-readonly-20261027/sdk-ops-live-100-02/`，需一起保留。模型是既有 `deepseek-v4-flash-0731`，实际记录 927 次调用、2,191,471 输入 token、172,955 输出 token，usage 缺失 0。价格、估算费用和供应商账单仍未知。

本批绑定的 core SHA 为 `ba924cba755a7669494cf1708b7ae56171337ef6dd32c2ffc9157057a9fb4879`，独立 ops SHA 为 `ce9828bef9fb49e214928eaeb1a6ba26ccd3b9c2e97f2b4bf9f0dd979cebda22`。最终全 SDK 回归 138 项通过；将来源身份改错、删去轮次、把 blocked 算作正确的隔离副本均被准入拒绝。

这只批准声明范围内的合成代码修复资格。模型结束后的本机真实财务门禁预检仍 blocked：未提供明确批准的历史案例 manifest，九平台均未验证；此结果不表示线上原件缺失，也没有发布收据。中断批次 `sdk-ops-live-100-01` 与各子集 smoke 均不出资格，已完成调用和中途未知 usage 单独保留，不与最终合格批混算供应商账单。
