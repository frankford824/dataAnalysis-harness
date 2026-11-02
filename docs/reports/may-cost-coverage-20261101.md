# 5 月样本商品成本覆盖率：可复核摘要（2026-11-01）

本文只记录隔离核算的结果和身份，不是上线收据，也不是财务批准。所有运行都在本机隔离目录完成，输入是只读原件、只读上游采集和冻结的模型；没有连接生产写入口，没有创建财务授权，没有改动原件、基线、期望值或阈值。逐目标明细、金额和订单号只保存在本机私有文件里，本文只给数量和原因码。

## 结果

| 平台/店 | 账期 | 应覆盖 | 已覆盖 | 覆盖率 | 仍缺 | 本轮变化 |
|---|---|---:|---:|---:|---:|---|
| 1688 alibaba1688_xingze | 2026-05 | 1260 | 1216 | 96.51% | 44 | 无新证据可接入（退货成员采集 0 条） |
| 抖音 douyin_qianhuajian | 2026-05 | 201 | 141 | 70.15% | 60 | 64.68% → 70.15%，新增 11 项来自仓库出库单 |
| 淘宝 taobao_xibishun | 2026-05 | 18450 | 10568 | 57.28% | 7882 | 未变 |

分母在所有运行中都没有改变。三家店的结账状态仍为不可结（`can_close=false`），净利润不可用。

## 运行身份

| 运行 | 输入摘要（SHA-256） | 说明 |
|---|---|---|
| 1688 combined-native-engine-preview-v5 | 输入清单 `13042388ec5654d1eab463ccdd3796fe1e07970b307270d805be1687c960e591` | 602 条清单项；原件不变；网络请求 0 |
| 抖音 dy-isolated-source-preview-v6-wms | `dca3c2f1902c6e41724c68d143ba6744fce496871a828940d20ac4b6194bc8f2` | 10 份原件、232 份历史价格采集、12 份仓库采集 |
| 淘宝 tb-normal-original-history-preview-v3-fixed-input | `271db41e0c73e7b62b0b7bb458b6be47d23112f0c0b543dde153abb6b8aaac89` | 投影逻辑 `3814d984167c7c49f1bab6ce758259840c43972c19edba8adba4108e823e3921` |

抖音 v6 的执行代码以文件哈希记录在私有输入清单里。代码基于提交 `1c268fa`，加上本次一并提交的 `source_wms_posting.py` 一行改动（平台定价模式缺失时进入既有的待定分支，而不是抛出 KeyError），运行所用文件哈希为 `21470807a6b3…`，与提交后的文件一致。运行前后有断言核对代码、模型和原件字节都没有变化。

## 抖音本轮接入了什么

此前抖音 176 个 ERP 源订单中，132 个已通过普通源成员合同证明，44 个待定。本轮把实现方已采集的 44 张仓库出库单据（12 个查询范围全部完整，单据版本冲突 0）交给仓库里已提交的合同 `source_wms_members.normalize_wms_members`：

- 11 个源订单通过，证明 23 个实物成员，依据全部是“当前已确认的出库单”。
- 33 个仍待定，原因都是 `native_original_bundle_action_missing`（原订单套装缺原始拆分动作）。合同没有放宽。

随后只对“所有源订单都没有普通证明、但全部有仓库证明”的目标，改用已提交的 `source_wms_posting.render_wms_preview_items` 生成隔离输入，其余目标仍走 v5 的原路径。核对结果：

- 141 个目标逐目标带符号金额核对通过（按现有分位输出相等）。
- 非商品成本的所有指标，逐源行（文件、行号、金额、是否计入、贡献）前后完全一致。
- 普通路径和仓库路径的原锚点、实物身份没有重叠。

**需要人签的限制**：仓库合同明确声明它不证明“没有退货”，也不证明确切发货时间（`proves_no_returns=false`、`proves_exact_dispatch_time=false`）。这 11 项能否作为商品成本依据，需要财务确认后才能进入正式核算。

## 仍缺项的原因

抖音 60 项：

| 原因码 | 数量 | 含义 |
|---|---:|---|
| source_member_proof_incomplete | 38 | 源订单成员未证明（含 33 个套装缺原始拆分动作） |
| full_history_price_not_covering_original_day | 17 | 历史价格区间不覆盖原下单日 |
| original_cost_source_missing_requires_native_only_contract | 3 | 没有原成本行，需要另建“仅原生记录”合同 |
| source_member_inventory_missing_or_unbounded | 2 | 成员库存缺失或无界 |

淘宝 7882 项：逐行原因（同一目标可能对应多行）为 `actual_normal_nongift_sent_item_unproved` 1123 行、`full_history_price_not_covering_original_day` 432 行、`full_history_price_requires_independent_zero_or_invalid_basis` 52 行、`existing_counted_authority_preserved` 8 行；其余目标仍待历史价格证据。淘宝另有 16 张仓库单据已采集，规模相对缺口很小，本轮未接入。

1688 44 项：在 v5 之前的 53 项分类为源单关系图不完整 27、仅原生记录待新合同 21、套装组件价不可转用 3、另行验证 2；v5 又覆盖其中 9 项，未重新逐项分类。

## 平台阈值建议（待决定）

模型现行结账门槛是商品成本覆盖率 95%（`chk_goods_coverage`，硬阻断），另有 `required_cost_unresolved` 对任何未解决的必需成本单独阻断。建议：

1. 门槛保持 95%，不为某个平台下调。1688 已达到，但仍被共享店铺归属和必需成本未解决阻断。
2. 抖音、淘宝短期内达不到 95%。不要无限期逐单补证；对这类店月，走系统已有的“确认本期成本”整期批准：由人按凭据批准店月三项成本总额，系统不把总额摊成逐单成本，个人提成保持待核。
3. 逐单补证继续作为后台任务，补齐后由系统自动核对是否与已批准总额一致（已有 `superseded_by_complete_sources` 机制）。
