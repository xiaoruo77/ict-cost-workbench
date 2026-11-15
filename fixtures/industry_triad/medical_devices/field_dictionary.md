# 医疗器械字段与覆盖字典 v1

所有数据为独立生成的抽象财务合成资料，期间2026-11、账簿SYNTH_MEDICAL_DEVICES、CNY两位货币。

| 项目 | 要求 | 样例状态 | 来源/口径与边界 |
|---|---|---|---|
| 零部件及外购 | 必需 | provided | raw_materials按原采购、领投料和关联原领料的退料估值，净投料进入批次parts |
| 装配人工 | 必需 | provided | finance:assembly_labor |
| 委外 | 条件必需 | provided | finance:outsourcing |
| 检测质检 | 条件必需 | provided | finance:quality为生产质检；registration_testing另作期间归属 |
| 返工 | 条件必需 | provided | finance:rework=60；dispositions批准单位身份逐行引用原返工凭证，与报废/退回全局互斥 |
| 包装 | 条件必需 | provided | finance:packaging，批次b单列 |
| 报废 | 条件必需 | provided | events:scrap两个批次10/5，同一事件只分出一次损失 |
| 库存与在制 | 必需 | provided | events双批次同质量桥，资产+损益=发生成本；不自动推等价完工程度 |
| 退货冲回 | 条件必需 | provided | events:return关联原sale，超过原量失败，回补库存并冲回净COGS |
| 仓储物流 | 条件必需 | provided | finance:warehouse_logistics=60，批准期间费用 |
| 设备折旧 | 条件必需 | provided | shared:depreciation资产基数÷12 |
| 公用/间接制造费 | 条件必需 | provided | shared:utilities，两阶段1:1分摊 |
| 注册或检测支出期间归属 | 条件必需 | provided | finance:registration_testing=120，仅体现合成企业批准口径 |
| 实际保修 | 条件必需 | provided | finance:actual_warranty=80，service对象，与准备金分开 |
| 正常售后 | 条件必需 | provided | finance:after_sales=40，独立service对象 |
| 保修/召回准备估计依据 | 条件必需 | missing | recall_basis_missing提示；无批准依据不计提，也不把实际支出当准备金 |
| 序列号逐台追溯 | 可选 | missing | serial_evidence_only：批次和文本凭证保留，不形成逐台追踪 |
| 批准单位处置防重 | 必需 | provided | dispositions单位身份为合成批准registry，返工/报废/退回互斥；不声称真实企业序列号逐台独立核验 |
| 现金/回款 | 可选 | provided | cash_only排除的支付备查；无收入结算模型 |
| 注册监管/税务/召回实施判断 | 不适用 | not_applicable | 不形成合规结论或医疗/生产操作建议 |

## 文件字段

| 文件 | 字段 | 校验与用途 |
|---|---|---|
| finance/shared.csv | id/economic_key | 唯一凭证与经济事项；重复、别名冲突应拒绝 |
| finance/shared.csv | book/period/currency | 必需，须与共同scope完全一致 |
| finance/shared.csv | kind/status/amount | 原始费用、应计、资产基数、预付基数或现金备查；金额十进制CNY两位，missing不能填0 |
| finance/shared.csv | tag/target_object_id | 版本化规则映射至成本组与对象，输入不含预计算总成本 |
| finance/shared.csv | evidence/origin_period | 合成凭证来源说明；摊销起期明确2026-01 |
| events.csv | id/object_id/op/quantity/unit/ref | 原始抽象投入/完工/销售/退回/报废数量；approved_unit不可与其他单位混用；return必须关联原sale |
| stock.csv（餐饮） | warehouse/item/uom/at/sequence/kind/quantity/receipt_value/original_issue_id/transfer_id | 独立即售包装商品pack单位；采购值不是当期费用，闭环调拨不是新增成本，退货须关联原销售 |
| raw_materials.csv | warehouse/item/uom/kind/quantity/receipt_value/production_object_id/economic_key/original_issue_id | 采购入库以approved_input_unit计量。生产/厨房领料扣减库存，关联原领料的退料回补库存。净耗用进入批次或菜品，原料物量与完工等价份额各有独立单位 |
| dispositions.csv（器械） | id/object_id/item_id/action/ref | 合成批准单位身份与返工/报废/退回互斥，ref分别关联原财务或生命周期事件；不是实际序列号追踪 |
| settlements.csv（餐饮） | id/object_id/op/amount/ref | gross为原记录，其余优惠/退款/佣金/手续费/回款均关联gross |
| rules.json | raw_composite.version=4 | 同一编译器多文件接入、共同账簿、跨源有序分摊；中间归集不能增加成本 |
| rules.json | business_views | 明示production/period/service，投入同质估值，未知/不适用由notices披露 |
| rules.json | business_views.required_costs | 逐对象声明关键成本组。有条件项按合成对象适用性声明，来源缺项不按零 |
| rules.json | object_budgets | 合成批准预算，成员及期间哈希绑定；不是实际企业预算 |
| expected.json | oracle | 独立整数分/有理数分摊与物量恒等式，不导入待测引擎 |

zero表示有明确零凭证/结算行，missing表示缺证据或未实现，not_applicable只对当前声明口径成立。
approved_unit是无生产含义的抽象同质等价单位；完工合格单位成本=分配至完工部分成本÷有来源的完工量。
库存物量价值按总投入的同质比例分配，保留在制、成品、净销售成本、报废四分区；不适用于不同完工程度或异质批次自动估值。
