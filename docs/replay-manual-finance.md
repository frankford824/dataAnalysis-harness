# 整期人工成本决定的隔离服务回放

现有业务只有 `manual-cost-preview` 和真实 `/close`。整期人工成本不存在独立的提前确认写入口。因此新增 `restore_approved_manual_finance` step 会在隔离 Workspace 中完成真实预览、认证和结账；成功意味着该隔离账期确实通过现有 Checker 并关闭。它不是向开放账期提前塞入人工成本的工具，也不连接生产库。

```json
{
  "operation": "restore_approved_manual_finance",
  "store_id": "target-store",
  "period": "2026-06",
  "approval_reference": "与案例完全一致的本次回放批准引用",
  "reference": {
    "path": "/absolute/path/historical-manual-decisions.private.json",
    "sha256": "原始捕获文件SHA256",
    "source_run_id": 328744,
    "decision_sha256": "原decision_json字符串的精确UTF8 SHA256",
    "original_result_sha256": "manual_finance.original_sha原字段",
    "decision_at": "原记录时间精确字符串"
  }
}
```

真实 `historical_golden` 案例还须在 `source_evidence.manual_finance_decisions_sha256` 绑定同一捕获文件 SHA。只有明确正向 `service_fixture` / `historical_golden` 允许执行；`source_rejection` 和拒绝夹具不允许执行，不能借人工金额把真实资料不足负例洗成正向。合成材料使用独立 method 标记，不能冒充真实只读 SQL 捕获。

真实材料读取原 `manual_finance.decision_json`：只使用其 `confirmed`、`base_costs`、早期批准 `payouts`、`no_payout` 和 `reason`。`observed`、`original_findings` 不作为独立金额 oracle；更晚的实发确认和银行到账仍是另一层，不回填原关闭决定。仅含提成、没有 `confirmed` 三科目的记录不能用此 step 恢复成本。

没有重演逐笔补录时，直接使用原已确认整期总额，不把总额拆成订单，不导入旧补录行。有当前逐笔补录时必须存在原 `base_costs`，且经过真实 API 加上当前补录后恰好等于原 `confirmed`；不一致立即拒绝，不能重复加成本或推测差额。真实 API 若根据当前兼职公摊自动校正了原批准提成，回放也拒绝，不静默产生新批准金额。

回放绑定冻结输入 fingerprint、当前临时 run、预览前后 Checker input SHA 和再次打开的原决定 SHA，随后调用真实 `api.close_period`，保留原始写入事务、认证人、证据、公司收入、公摊和冻结门禁。不会直接 INSERT `manual_finance`、关闭后重开，或传入 ignored blockers。材料在最终 snapshot 时再次核验。

`original_sha` 在实际业务写入中是 `sha256(run.result)`，不是原件清单 input SHA。旧 run ID 映射到本次临时 run，只证明文档引用与来源一致，不能宣称历史输入已完全重建。旧捕获没有独立 authenticated actor/session；本次临时人工会话仅为隔离技术重演，不补造旧身份认证、不赋予生产权限。真实黄金案例的批准仍须单独审核，新增 step 和合成测试本身不提供金融上线资格。

真实历史决定文件必须位于本次 bundle 内，才能沿用发布报告工具对整个 bundle 的覆盖保护。应复制为新 bundle 版本，再将原 SHA 完全一致的捕获文件放入 `evidence/`，重新 `frozen.seal` 并审核新案例；不要往旧封存目录追加后仍宣称旧 manifest 未变。业务原件及旧 bundle 保留，辅助决定材料由新 manifest 完整列入文件清单。
