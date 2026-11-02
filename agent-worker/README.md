# Ledger AgentScope Worker

Worker 使用固定版本 `agentscope==2.0.11` 执行案件取证工具循环，复用现有 host-agent 传输协议、租约和持久回执。是否完成由台账 Checker 重新核对当前输入决定。模型回复、取证成功或提交完成请求，都不构成月结证明。

本文提供本地配置步骤；它不是生产部署、模型已通过标定或 M6 观察完成的证明。示例中的账号名、模型名、域名和 `/opt/example`、`/srv/example`、`/etc/example` 路径均为虚构占位值，必须按目标机器替换。不要把明文 token、密码、真实原件或私有评估审计目录提交到仓库。

## 1. 独立安装环境

需要完整仓库源码和独立 Python **3.12** SDK 环境；worker 通过 HTTP 调用台账服务。以下是从源码安装 host-agent 和 worker 的基础步骤，在 Linux 仓库根目录执行：

```bash
cd /opt/example/dataAnalysis
python3.12 -m venv /srv/example/ledger-worker/.venv
. /srv/example/ledger-worker/.venv/bin/activate
python -m pip install -e ./host-agent -e ./agent-worker
export PYTHONPATH="/opt/example/dataAnalysis/ledger:/opt/example/dataAnalysis/host-agent:/opt/example/dataAnalysis${PYTHONPATH:+:$PYTHONPATH}"
python -m pip check
python -c 'from importlib.metadata import version; assert version("agentscope") == "2.0.11"; print("agentscope", version("agentscope"))'
```

本次发行已提供 Linux / Python 3.12.3 实测传递依赖：`agent-worker/requirements.lock`。
从仓库根用 **3.12** 解释器执行 `python tools/bootstrap-ledger-agent.py --venv /srv/example/ledger-worker/.venv`，
或对已安装环境使用同命令的 `--check-only`；它会校验 SDK 版本、依赖及源码导入，并拒绝财务服务的现有虚拟环境路径。
Windows 原生 SDK 环境仍需用相同 lock 单独验证；本次真实网关和 SDK 完整回归使用 WSL/Linux 环境。

`agent-worker/pyproject.toml` 固定 SDK `2.0.11` 和 `httpx==0.28.1`。正式交付使用上述 lock 和 `tools/bootstrap-ledger-agent.py`，不要直接把财务服务的整份 `ledger/requirements.txt` 合并进 SDK 环境。该 lock 已在 Linux / Python 3.12.3 验证；其他平台必须分别验证，不能把 Linux 导入检查当作 Windows 上线证明。

必须保留仓库的本地源码布局和对应 `PYTHONPATH`，CLI 会复用相邻的 `host-agent`、`ledger` 模块；上面的 `PYTHONPATH` 只添加源码目录，不混入服务虚拟环境的第三方包。认证 CLI 使用服务端已有的 ledger 虚拟环境，见第 4 节。

Windows 可用 `py -3.12 -m venv` 建立独立环境，再用该环境的 `Scripts/python.exe -m pip` 安装。配置中的路径须改为运行进程可访问的本地绝对路径，JSON 中反斜线写成 `\\`，也可使用 `C:/...`。WSL 进程使用 Linux 路径；Windows 进程使用 Windows 路径。Linux 的 `chmod` 权限步骤在 Windows 上应改为对应用户的文件 ACL。

## 2. 继承已有模型网关

示例 `inherit_ledger_gateway: true` 复用**运行 worker 的本机、同一运行用户**的 `ledger.assist.load_config()`，不会探测其他机器或复制浏览器会话。`load_config()` 使用 `LEDGER_LLM_BASE_URL`、`LEDGER_LLM_MODEL`、`LEDGER_LLM_API_KEY`，缺项再从现有台账工作区 `llm.json` 及其 `api_key_file` 读取。工作区由 `LEDGER_HOME` 指定，未设时为当前用户的 `~/.ledger`。

如果现有网关属于另一个工作区，先把 `LEDGER_HOME` 指向那一个已有目录；不要用空目录替代已有配置，也不要将网关密钥写进 worker JSON：

```bash
export LEDGER_HOME=/srv/example/ledger-data
```

worker CLI 的地址优先级为 JSON `base_url` → `FA_GATEWAY_BASE_URL` → `LEDGER_LLM_BASE_URL` → 继承配置；密钥优先级为 `FA_GATEWAY_API_KEY` → `LEDGER_LLM_API_KEY` → JSON `api_key_file` → 继承配置。继承只补缺项。下面的标定命令直接使用 `ledger.assist` 的现有配置，不读取 `FA_GATEWAY_*` 或 worker JSON；按本文继承模式运行时，不要另设这些覆盖项，以免标定和执行使用不同网关。

## 3. 先标定，再填写 models

在代码和依赖稳定后，用目标机器现有网关运行真实模型调用。将模型占位名改成该网关实际可用的模型名。输出目录必须不存在，每次复测使用新目录：

```bash
python -m ledger_agent.evaluate \
  --output /srv/example/ledger-worker/evaluations/evidence-20261027-01 \
  --rounds 5 \
  --model example-evidence-model \
  --price /srv/example/ledger-worker/prices/example-evidence-model.json
```

`--price` 必填，内容为供应商当前标价（人民币，每百万 token）：`{"version":"供应商价目日期","currency":"CNY","input_per_million":"…","output_per_million":"…"}`，有缓存单价时另加 `cached_input_per_million`。业务 Agent、运维修复和三个评估器共享同一 `state_dir` 时共用一个月度上限：默认且最高 2000 元（按上海时区自然月），配置 `"monthly_budget":{"amount":"…","currency":"CNY"}` 只能调低。每次调用前按"全部输入字节都算未缓存 token、输出按上限"预留最坏费用，超出即不发送，案件进入 `waiting_configuration`（原因 `monthly_budget_exhausted`）；没有单价的模型直接不调用（原因 `model_price_unknown_under_cap`），未知价格不按零计。`--usage` 输出中的 `budget.committed` 是本月已占用额度。

评估器覆盖二十个声明的合成案件，每案五轮；这些是带真实 provider 调用的合成取证用例，会产生模型调用费用。`evaluation.json` 始终记录结果，只有全部通过才生成 `model-profile.json`。`production_corpus_verified: false` 表示未验证真实财务语料；不能据此声称生产历史缺口已修复。`--case` 子集或少于五轮的调试结果不能用于运行时准入。

先复制示例并将 URL、身份及绝对路径改成目标机器值；`models: []` 故意保持未标定状态：

```bash
umask 077
mkdir -p /srv/example/ledger-worker
cp agent-worker/worker-config.example.json /srv/example/ledger-worker/worker.json
```

只有 `passed: true`、报告文件 SHA、SDK 版本、模型名、所需技能、当前 `runtime_sha` 和当前网关 `provider_sha` 均匹配的 profile 才可进入 `models`。下面在继承模式下先调用运行时自己的准入检查，再把生成的完整 profile 写入配置；从仓库根目录执行：

```bash
python - /srv/example/ledger-worker/worker.json \
  /srv/example/ledger-worker/evaluations/evidence-20261027-01/model-profile.json <<'PY'
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path("ledger").resolve()))
from ledger.assist import load_config
from ledger_agent.runtime import Config, ModelSpec

config_path, profile_path = map(Path, sys.argv[1:])
raw = json.loads(config_path.read_text(encoding="utf-8"))
if (not raw.get("inherit_ledger_gateway", True) or raw.get("base_url") or raw.get("api_key_file")
        or os.environ.get("FA_GATEWAY_BASE_URL") or os.environ.get("FA_GATEWAY_API_KEY")):
    raise SystemExit("此步骤要求使用与评估器相同的 ledger.assist 继承配置")
saved = load_config()
if not saved.ready:
    raise SystemExit("已有 ledger 网关配置不完整")
profile = json.loads(profile_path.read_text(encoding="utf-8"))
Config(saved.base_url, saved.api_key, (ModelSpec(**profile),)).select(("evidence",))
raw["models"] = [profile]
config_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print("evidence profile 已核验并写入配置")
PY
```

不要手改 `evaluation.json`、`evaluation_sha`、`skills` 或 `passed` 来通过准入。`evaluation_path` 是该机器上的绝对路径；移动评估材料、修改运行时/技能文件或更换网关地址后，需要重新核验，必要时重新评估。当前 runtime 身份绑定 `runtime.py`、`privacy.py`、`state.py` 和 evidence skill；provider 身份绑定去掉末尾斜线的网关地址，**不证明网关背后的模型权重、路由或生产语料表现相同**。profile 中的 `price` 来自评估时的 `--price` 文件，`cost_per_correct` 按该价目对全部调用估算；有调用缺用量时保持 `null`，不能据此宣布最低成本。

## 4. 建立人工账号与独立服务身份

在台账服务所在机器使用 `tools/ledger-auth.py`，解释器明确选择服务端**已有的 ledger 虚拟环境**：Linux 为 `ledger/.venv/bin/python`，Windows 为既有环境的 `ledger/.venv/Scripts/python.exe`。下例使用虚拟的绝对源码路径；请替换为目标机器实际路径。认证 CLI 不使用 bundled Python 或 worker SDK 虚拟环境。这些命令由操作者在配置时执行；本文及示例不会创建真实账号。

人工账号用于浏览器登录后取得 human session。`--home` 必须指向台账实际工作区，不能误指 worker 的 `state_dir`。默认账号不是管理员；只有确需管理员时显式增加 `--admin`。不提供 `--password-file` 时通过 getpass 输入，密码不得出现在命令行：

```bash
/opt/example/dataAnalysis/ledger/.venv/bin/python /opt/example/dataAnalysis/tools/ledger-auth.py operator-add \
  --home /srv/example/ledger-data \
  --name example-finance-operator
```

批量配置可改为 `--password-file /etc/example/ledger/operator-password.input`，读取预先安全保存的密码文件；密码至少十二位。Linux 文件应归当前用户所有、权限为 `0600` 或 `0400`，Windows 使用仅当前用户可读的受保护 ACL。不要在脚本中写示例明文密码。

工具对本次新建的 home、commission 目录和认证数据库设置私有权限：Linux 目录 `0700`、数据库 `0600`；Windows 使用仅当前用户可访问的 ACL，并为新目录设置继承。既有目录及数据库的权限保持原样，**不会自动迁移既有权限**；应结合既有服务运行账号单独核对。

服务身份由服务端身份 JSON 和 worker 专用明文 token 文件组成。服务端只保存 token 的 SHA256；`LEDGER_SERVICE_IDENTITIES` 指向**服务端**身份 JSON。下面仅授予 worker 协议能力，身份 `--id` 必须与 worker JSON 的 `worker_id` 完全一致：

```bash
/opt/example/dataAnalysis/ledger/.venv/bin/python /opt/example/dataAnalysis/tools/ledger-auth.py service-issue \
  --identity-file /etc/example/ledger/service-identities.json \
  --token-file /etc/example/ledger-worker/service-token \
  --id example-ledger-worker \
  --capabilities agent_control \
  --scopes business \
  --expires-in 2592000
```

将下面的环境配置注入台账**服务进程**；只在交互 shell 中设置不会改变已运行服务的环境：

```bash
export LEDGER_SERVICE_IDENTITIES=/etc/example/ledger/service-identities.json
```

`--capabilities` 和 `--scopes` 都接受逗号分隔列表。当前服务路由允许的 capability 为 `intake,recompute,freight_experiment,advance,approved_action,agent_control`；scope 为 `business,ops`。这不表示每个能力都应授给同一服务身份。重复签发同一 ID 默认拒绝；需要轮换时使用相同完整参数并显式加 `--rotate`，然后协调更新 worker token 文件与服务端身份文件。明文 token 只交给指定 worker，不放入模型上下文。

`service_token_file` 读取上述 worker token；若同时设置 `FINANCE_AGENT_ACCESS_TOKEN`，环境变量优先，旧环境值可能掩盖已轮换文件。只读检查现有配置：

```bash
/opt/example/dataAnalysis/ledger/.venv/bin/python /opt/example/dataAnalysis/tools/ledger-auth.py doctor \
  --home /srv/example/ledger-data \
  --identity-file /etc/example/ledger/service-identities.json
```

`doctor` 至少接受 `--home`、`--identity-file` 之一，可同时传入；不创建工作区、账号或 token，也不证明网络连通和业务验收通过。账号检查把原数据库及存在的 WAL、journal 复制到私有临时目录，核对复制前后及查询后的原件 SHA 稳定性，再查询副本；SQLite 恢复和辅助文件只发生在临时副本中，不在原目录创建 `-wal/-shm`，也不会忽略活跃 WAL 中已提交的账号变化。原件变化时返回 `registry_snapshot_changed_retry`，保持未确认并等待重试。

D4 的确定性规则默认由服务端 `LEDGER_DETERMINISTIC_AUTOMATION_ENABLED` 关闭。确需启用受控历史成本动作时，必须先由已登录的人工通过现有决定 preview/apply 批准相应 `rule_grant`、店月范围与有效期，再给服务身份配置 `business` scope 和 `approved_action` capability。开开关或签发 token 都不能替代人工授权。SDK、worker token、ops 身份均不能批准规则、结账、反结账、实发或修改财务政策。详细约束见 [月度自动化执行合同](../docs/automation-contract.md)。

## 5. Collector 上游配置

`worker-config.example.json` 的 `upstream.order_token_file` 和 `upstream.cost_token_file` 分别读取订单台集成 token 与历史成本桥 token。它们与 worker 服务 token、模型网关密钥是独立凭据。文件不配置时，CLI 分别退回 `LEDGER_ORDER_FEED_TOKEN` 和 `ERP_BRIDGE_COST_API_TOKEN`；文件路径存在但不可读会报错，不会从浏览器或历史脚本搜凭据。

`ledger_base_url` 默认采用 `control_plane_url`，`ledger_token` 由 CLI 注入 worker 服务 token。三个 base URL 必须是可信 HTTP(S) origin，不含路径、查询、片段或内嵌用户名密码。示例成本域名 `.invalid` 故意不可用，必须替换。`member_requests_enabled` 默认 `false`，显式保持关闭；另行完成上游补采授权与回流验证后才可设 `true`。这个开关只控制成员证据排队 POST，排队成功不等于上游证据已回流或案件完成。

默认四个工具不接受模型生成的金额或任意 URL；请求身份来自当前案件和封存 run。原始上游响应留在本机私有证据目录，模型获得状态和不透明引用。模型网关仍可能向外部 provider 发送经过检查的上下文，不能宣称所有模型数据均留在本机。协议、配额、缺失/冲突与未完成状态见 [上游取证合同](docs/upstream-evidence.md)。示例 `tools: {}` 不注册额外本机命令。

## 6. 启动与观察

确认身份、网关、已通过的 profile 和上游配置后，先执行一个轮询周期；此命令会向服务发送心跳并可能认领、执行案件，不是只读检查：

```bash
ledger-agent --config /srv/example/ledger-worker/worker.json --once
```

去掉 `--once` 后持续轮询。`ledger-agent --config /srv/example/ledger-worker/worker.json --usage` 查看本机用量汇总；首次打开 state 时会初始化本机状态。`waiting_configuration` 表示缺网关或合格模型；`waiting_evidence`、`login_required`、`pending`、`conflict`、租约/输入失效或 `maintenance` 都不能上报业务成功。Worker 的成功回执还必须被服务端独立 Checker 接受。

## 7. ops 修复与 M6 的独立门槛

`ledger_agent.evaluate` 只批准 `evidence`；独立 `ledger_agent.ops_evaluate` 使用二十种合成代码故障及外部父进程 oracle 标定 ops。不能给 evidence profile 改 skills 冒用。`--repair-event` 在创建工作树前要求通用 core 身份与独立 ops 身份、完整证明均通过；校准配置不能作为生产准入。评估命令、证明保留和“代码已验证／发布仍阻断”的区别见 [ops 标定合同](docs/ops-evaluation.md)。示例仍不自动填写已批准 ops 模型或真实执行器配置。

具备独立 ops 标定后，修复仍需 Sentinel 的隔离工作树、固定 checks、独立可信验收器与真实语料验收。只有可用 bwrap 断网环境或 SHA 固定的 Docker 镜像可执行检查；没有网络隔离后端、隔离启动失败或缺少可信测试报告时应为 `blocked`，不能回退宿主 shell。修复产物 `reviewable` / `published: false` 只表示可审查，不能称为已发布。配置合同见 [Sentinel 隔离修复](../ops/ledger_sentinel/README.md#agentscope-隔离修复适配)。

M6 的 shadow、canary、production 开关默认关闭；本 worker 示例不启用这些阶段。启用 shadow 不增加观察计数。升 canary 需要同一代码与业务规则模型身份下、两个不同业务账期的完整 shadow 观察；升 production 需要两个完整 canary 观察周期。默认每周期至少 86400 秒且至少两次采样，还需金额守恒、历史冻结不变、范围隔离、必需输出、公开读回、回滚演练、worker 健康的原始材料和校验值。这里的 `model_sha` 是发布验收中的业务规则模型摘要，不是 LLM 名称或 `model-profile.json` 的 SHA。

缺实际语料验收、缺周期、材料变化或检查失败时必须保持 `blocked`。单元测试、本地演练、模型合成评估、配置开关和一次部署成功都不能充当两个生产观察周期。本文未记录任何真实生产观察，也未创建服务、计划任务或执行发布。
