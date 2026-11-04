# 业务口径与公式（模型 models/cn-ecommerce）

## 基本对象
- 店铺 store：57 家（pdd 22、taobao 19、douyin 6、offline 3、1688 2、other 2、jd/kuaishou/xiaohongshu 各 1；含 5 家 legacy_pdd_* 历史归档）。店名格式「负责人-平台店名」，如 taobao_xibishun=汪学成-天猫喜必顺旗舰店（别名 淘宝喜必顺）。id/name 不改，改名用 alias。
- 账期 period：YYYY-MM；另有「(未知账期)」。店月 = 一家店一个月。
- 状态：open 开放 / closed 已结账（冻结）。stale=结账后又来了新资料（不会自动改历史）。
- run：一次核算结果（引擎 Polars）。已结账月展示 period.run_id 的冻结结果；人工结账存 manual_finance。

## 损益表节点（/api/trend 和店月 statement 的 id；值=对利润的贡献，成本为负）
| id | 名称 | 说明 |
|---|---|---|
| n_receipt | 销售收入 | 对账资金流水的交易收款（headline 收入） |
| n_refund | 销售退款 | 按资金流水发生月入账 |
| g_revenue | 收入 | n_receipt+n_refund |
| g_platform | 平台费用 | n_expense 平台综合支出(1688)、n_software 平台服务费、n_logistics 物流运费、n_cross_border 跨境服务费、n_compensation 交易赔付、n_insurance 权益保险(抖音)、n_marketing 平台营销费用 |
| g_goods | 商品成本(组) | n_goods 商品成本(聚水潭/订单台，含退货成本冲回)+n_dropship 代发成本+n_reshipment 补发成本 |
| g_fulfillment | 履约成本 | n_freight 发货运费 |
| g_promotion | 营销投入 | n_ad 推广费用+n_small_payment 客服打款(小额打款)+n_brushing 本金佣金(刷单/补单) |
| gross_profit | 毛利 | n_receipt+g_goods（**不扣退款**） |
| net_profit | 利润 | g_revenue+g_platform+g_goods+g_fulfillment+g_promotion（headline 利润，提成基数） |
| net_margin | 利润率 | net_profit/n_receipt |

## 看板口径（/api/operations/month metrics；成本为正数）
- receipts 对账销售收入（未扣退款）；refunds 销售退款；net_income 销售收入（到账）=receipts−refunds
- goods_cost 商品成本=n_goods+n_dropship+n_reshipment；promotion_cost 推广费用=n_ad；platform_cost 平台费用=g_platform；freight_cost 发货运费=n_freight
- aftersales_cost 客服售后费用=small_payment_cost 客服打款+brushing_cost 本金佣金（补单）
- gross_profit 商品毛利=net_income−goods_cost（**扣了退款**，与损益表“毛利”不同！）
- profit 利润（扣兼职前）=商品毛利−推广−平台−运费−客服售后 ＝ 损益表 net_profit
- labor_cost 兼职人工费用（公司兼职总额按各店当月销售收入占比分摊）；profit_after_labor 利润额（扣兼职后）=profit−labor_cost
- 比率：refund_rate=refunds/receipts；goods_rate 货本率、gross_margin 毛利率、promotion_rate、freight_rate、profit_margin、labor_rate、after_labor_margin 均以 net_income 为分母。
- commission 已保存提成。任一组成为 null → 结果 null（不补零）。

## 资料（sources.yaml）
结账必需：订单明细(order_detail，主干)、对账/资金流水(settlement)、聚水潭成本(order_cost，K表「聚水潭成本（补充）-店名.xlsx」并入)、运费(freight，公司级周表，按运单号匹配)。
可选：售后单(after_sales 公司级)、推广(按商品ID)、刷单/补单(本金佣金，公司级)、代发(公司级)、补发、权益保险(抖音)、小额打款(公司级)、退货成本冲回(派生)。
上传规则：文件名「类别-店铺.xlsx」，同名=新版本替换；NAS X:\台账系统\00_上传区 → 10_已接收（认不出→20_需修正，旧版→90_历史版本）。

## 结账检查（checks.yaml）
chk_goods_coverage 商品成本覆盖率≥95%（阻断）；chk_receipt_coverage 收入挂单率≥95%（阻断）；chk_settlement_link 挂钩率 0.90（提示）；chk_no_unclassified 已分类≥99.5%（阻断）；chk_major_has_metric（阻断）；chk_sources_ready 必需资料到齐（阻断）；chk_unlinked_disclosed（提示）。
覆盖率=有成本的应计订单/应计订单数（取消/关闭不计；刷单零成本需有标记）。

## 未挂单 4 类（unlinked_buckets）
其他店的数据｜非经营流水（规则已排除）｜其他账期的订单｜取不出订单号要查归属（只有这一类需处理）。

## 提成
- 规则按 店铺×商品ID，总提成率固定，多人子提成率之和=总率；按下单时间匹配生效时间段。
- 身份 duty：做货 produce（产出归他）/抽点 cut（不占销售/毛利/利润）。多人做货按点数分。
- 基数 commission_base=net_profit（实际扣兼职后经营利润），commission_on_loss=deduct（亏损扣负）。
- 应发=系统算；实发可编辑（payout_confirmation 核定实发，不可改）。组织：团队-组-人。
