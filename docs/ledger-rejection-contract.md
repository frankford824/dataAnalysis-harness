# 独立拒绝验收合同

`Session.check(expected)` 和现有 `verify-ledger-service-cases.py` 继续承担正向金额回放。`n_receipt`、`gross_profit`、`net_profit` 的黄金期望仍须为有限且可用的金额。

`Session.check_rejection(expected)` 验证明确的不完整、等待或拒绝。通过表示拒绝断言成立；返回值固定 `positive_scope_count=0`、`ready_scope_count=0`、`closed_scope_count=0`、`eligible_for_financial_release=false`。它不生成发布收据，也不能替代完整正常店月正向验收。

## 范围与观测

Session按固定 `publication_scope` 枚举所有批准店月，不能从已有run推范围。`snapshot.observations` 包含每个批准scope；无run的店仍存在于观测清单，`snapshot.scopes` 中则不伪造金融结果。

观测明确区分：

- `not_started/not_attempted`：这个隔离基线还没有执行核算，不能算成已经处理完资料。
- `no_run/no_published_result`：已尝试，但该批准店月没有发表结果。
- `pending`：有run但Checker尚未允许完成；`source_state`另示同步、缺资料或已捕获输入。
- `failed`：服务显式返回了已核对的失败类型。
- `rejected`：明确业务入口拒绝了请求。

`reason_codes` 比较完整、排序去重的Checker原因码集合。技术源码/Model/输入身份漂移、证据损坏和权限边界不能冒充“缺业务资料正确拒绝”覆盖。

公司来源观测将完整 `basis_total` 与 `known_income_subtotal` 分开：仍有收入未知的店时，完整分母为None；已知部分的小计可以为0，不能用该小计替代完整分母。月兼职总额未知也保持None。

## 拒绝期望

期望带 `schema_version=1`、`category` 和全部批准 `scopes`。每个scope必须分别列 `observation/proved/unknown`；只比较有独立依据的金融字段，不要求编造完整店月金额。

```json
{
  "schema_version": 1,
  "category": "incomplete_data",
  "scopes": {
    "example_store/2026-09": {
      "observation": {
        "run_present": true,
        "state": "open",
        "outcome": "pending",
        "source_state": "captured",
        "checker_allowed": false,
        "reason_codes": [
          "commission_incomplete",
          "cost_decision_required",
          "profit_unknown",
          "required_cost_unresolved"
        ],
        "failure": null
      },
      "proved": {
        "statement": {"n_receipt": {"value": 200, "available": true}},
        "can_close": false
      },
      "unknown": {
        "statement": {
          "gross_profit": {"value": null, "available": false},
          "net_profit": {"value": null, "available": false}
        }
      }
    }
  }
}
```

例中数字是合成合同示例，不是实际财务期望。真实期望须绑定独立原始控制材料，禁止从候选核算输出录制。

金额未知必须同时为 `value=None/available=False`；不能用0或已计部分填充完整金额。缺字段也不等于None。空人员映射是明确的空集断言，不是任意人员的通配符。同一字段不能同时声称已证与未知。

额外补录总额与外部商品成本来源不同：它来自本地唯一、只追加的受控补录日志。有已留档run，且完整日志或该run捕获的revision证明没有已计补录时，`cost_line_total=0`是已知空集；无run不造金融字段，已有save但未证明计入当前run则保持None。不得把这个闭合来源的空集规则用于广告费用、收入或兼职资料缺失。

`category=scope_coverage` 只验证批准清单完整，例如52店的未计算基线，不提供金融字段且真实资料拒绝覆盖为0。`safety_boundary` 单独验证明确权限或完整性拒绝，同样不给正向金额或真实资料拒绝覆盖。

## 明确失败

`expect_failure(operation, arguments, expected)` 只捕获服务显式返回的 `ReplayServiceFailure`、真实业务入口的 `ReplayActionRejection` 或具体领域拒绝；异常类型、kind、code必须吻合。没有稳定错误码的领域拒绝还必须吻合明确message。任意ValueError、程序异常、输入身份错误不会因为“发生失败”就通过。

缺必需成本的20/19案例保持95%质量线通过，但经济成本不完整时毛利/利润未知并拒绝结账。合法补录2元后利润恢复160；明确核定三类整期成本又是独立人工流程。不能用无凭据豁免替代这两条路径。

## 独立工具

`tools/verify-ledger-rejections.py` 读取SHA绑定的 `source_rejection_suite`，核对案例、全部原件、执行Model、源码、固定配置和独立控制材料的前后身份。默认拒绝合成材料；`--allow-fixtures` 仅供合成合同测试，永不增加真实来源覆盖。

该工具没有录基线/批准金融发布模式。CLI非零与JUnit失败必须一致，空suite失败，未知异常也失败。原14项历史尝试、原十项失败、阈值与旧baseline继续保留；新增真实negative不能使旧positive断言追溯通过。
