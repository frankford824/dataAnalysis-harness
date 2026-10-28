# 受控源恢复

源恢复是只读能力，复用原有 case、attempt、认领令牌和心跳。已知缺订单先由 Worker 确定性 preflight 取证；服务端从封存 run 推导查询身份并返回持久任务 ID，父 attempt 释放后再认领固定源任务。此路径不占模型轮询。真正需要判断时，才检查相应模型的 source profile，判断最多六轮；通用 SDK 和运维模型保留各自的资格。

## 数据与权限边界

- 模型不能提交金额、日期、SKU、任意路径、URL、SQL 或命令；请求只选择当前封存缺口。
- 新 run 的不可变 proof 保存必要原件索引。原件 SHA、工作表和物理行号只定位证据；后端仍独立重读原始字节。旧 run 无索引时不补签。
- `order-feed:*` 是派生身份，不能冒充原工作簿 SHA。缺原工作簿时，已封存缺口可以授权有限原日查询；它不自动成为成本事实授权。
- 固定 Node 入口只访问已验证的 JST 查询、明细和关系页面，单并发、至少一秒间隔，不自动刷新登录、不启动全库采集。原始响应按 SHA 保存并 fsync。
- `source_raw_ready` 不等于源成员证明通过，更不等于财务就绪。日期、数量、价格、政策和财务完成分别核验。
- 原单日期可以早于当前账期。读取完整源数据，当前自动发布仍由 `PublicationScope` 限定；首批为获批的 2026-09 范围。不能用源日期授权修改旧月。

## 配置

服务端 `LEDGER_SOURCE_RECOVERY_PERIODS=2026-09` 只开放相应 case 的源读取；它不授予财务写入权限。另须配置并验证当前自动发布 scope 文件。

Worker 配置示例（以下路径须按实际已安装运行时核对）：

```json
{
  "source_recovery": {
    "workspace_root": "D:/ledger/home",
    "node_binary": "D:/runtime/node.exe",
    "jst_module": "D:/order/collector/jst.mjs",
    "jst_sha256": "0934850fd166d44a5e28570d1bc4b49484c9815ab84b8cbd2d5f36669bb3acdd",
    "enabled": false,
    "profile_path": "D:/ledger/qualification/source-evaluation.json",
    "profile_sha256": "REPLACE_WITH_VERIFIED_REPORT_SHA256"
  }
}
```

该 JST SHA 是本轮实际读取的客户端版本，不是永久兼容承诺。客户端变化需要重新审查。模型配置仍复用既有网关，不在示例中保存密钥。

固定读取能力的资格与当前模型是否启用分开：模型关闭时，已通过资格的确定性读取仍可执行；进入模型时额外验证模型名称、供应商和新 source profile。生产命令禁止使用 evaluation 开关绕过资格。

## 日期消费

`source_dates.py` 根据精确单原平台父单、已确认店铺、普通原单身份和唯一原日期证明日期。后来被合并的原始节点仍可作为证据；多父收敛节点不能广播日期。日期证明不额外要求成本或发货数量 DAG 完成。

通过当前 case、输入及发布范围检查后，服务端追加 `date_source_fact` 与已有 outbox，由原 Manager 推进。读取时再次验证原 bytes，并复用 `refund_order_dates_v1`；不会生成订单、销售或成本。每个原父单独生成稳定输入，其他月份增加原单日期不会改变本单的 CSV SHA 或行引用。

已验证日期是可复用来源事实，原触发 case 月份不是永久读取限制。人类合法重算也读取这些事实；自动结果发布始终经过本次目标 scope。

## 恢复与验收

- 无 Node、会话失效、字节或时间预算、过期输入、租约丢失均保留真实 pending/maintenance。
- 心跳只传配置及会话的单向摘要。宿主恢复后，在新的配置 epoch 下重试同一持久任务；原失败收据不覆盖。
- 通用与运维模型每轮都在所属事件循环关闭公开 AsyncClient。必需回归包括真实 Worker 连续新循环的成功、HTTP 失败、超时和 Agent 构造失败；仅在一个长 async loop 下评估不能替代这些检查。
- 运行 `python -m ledger_agent.source_evaluate`，至少二十案例、每例五次。逻辑运行数、确定性零模型运行、真实网关调用、停服探针和 token 分开报告。合成 I/O 资格不是财务黄金样本，也不是生产结账观察。
- 任何来源漂移、未完成评估或中断尝试均不能准入。调用及 unknown usage 全部保留，报价和结算未知时不能报告人民币实付为零。

成本 v3 的预览、授权及采用独立于原日输入。后端通过 `verified_source()` 重读原件、原 raw 与完整原字段；缺失标志保持 unknown，不能将 Split 改成 Sent，不能默认 `is_suspect`、gift 或 virtual 为 false。
