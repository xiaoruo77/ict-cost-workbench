# 制药字段与覆盖字典 v1

所有数据为独立生成的抽象财务合成资料，期间2026-11、账簿SYNTH_PHARMA、CNY两位货币。

| 项目 | 要求 | 样例状态 | 来源/口径与边界 |
|---|---|---|---|
| 原辅包材 | 必需 | provided | raw_materials原辅料采购和可追溯领退料进入批次materials。finance:packaging单列本期包材耗用 |
| 直接人工 | 必需 | provided | finance:labor为accrual，支付备查cash_only不重复确认 |
| 外协 | 条件必需 | provided | finance:outsourcing |
| 检验与放行 | 条件必需 | provided | finance:quality/release，两个不同成本组 |
| 研发及试验 | 条件必需 | provided | finance:research_expense，批准全部为期间费用，无自动资本化 |
| 公用工程 | 条件必需 | provided | shared:utilities→bridge→批次，2:1批准动因 |
| 设备折旧 | 条件必需 | provided | shared:depreciation原资产基数÷12，仅本期摊销进入成本 |
| 许可与预付摊销 | 条件必需 | provided | shared:licence_amortization预付基数÷12，原值不整笔费用化 |
| 间接制造费用 | 必需 | provided | 共享源多阶段归集；原成本组不因bridge改名 |
| 批次报废 | 条件必需 | provided | events:scrap只从在制扣，样例批次a报废10，b显式无该事件且量桥可核 |
| 成品退回/销售冲销 | 条件必需 | provided | events:return关联原sale，回补成品，净COGS冲回，不新记第二笔退回费用 |
| 生产投料退回 | 条件必需 | provided | raw_materials:production_return绑定原领料并回补库存；本期采购额等于批次净投料加原料期末余额 |
| 在制/成品/销售成本 | 必需 | provided | events同质投入120/90、完工100/80，财务发生成本分区守恒 |
| 批准投入/合格/放行量 | 必需 | provided | 抽象approved_unit，投入及完工来自events原始行；无配方或实验操作 |
| 仓储物流 | 条件必需 | provided | finance:warehouse_logistics，按合成企业批准为期间支出 |
| 结算/现金流 | 可选 | provided | cash_only为排除的支付备查；本行业样例无收入结算模型 |
| 税务/GMP/自动资本化 | 不适用 | not_applicable | 本轮抽象财务验证不形成监管合规或税务判断 |

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
