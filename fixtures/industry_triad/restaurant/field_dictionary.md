# 餐饮字段与覆盖字典 v1

所有数据为独立生成的抽象财务合成资料，期间2026-11、账簿SYNTH_RESTAURANT、CNY两位货币。

| 项目 | 要求 | 样例状态 | 来源/口径与边界 |
|---|---|---|---|
| 采购及供应商结算 | 条件必需 | provided/missing | raw_materials厨房与stock即售商品分别保存采购入库原值。供应商付款只保留现金备查 |
| 配方/净料/逐单领用 | 条件必需 | missing | native_pos_unimplemented：不从POS或配方自动推导可靠领用 |
| 成本对象与批准食材耗用 | 必需 | provided | raw_materials原始入库、领料和退料生成ingredients_consumed，两个门店×堂食/外卖×rice（米饭类）/noodles（面食类）共八对象 |
| 领用/调拨/盘点 | 条件必需 | provided/missing | 厨房领料及关联原领料退库有来源。stock即售商品跨店闭环调拨10元、盘点损耗4元。厨房原料跨店调拨/盘点未纳入样例 |
| 报损 | 条件必需 | provided | events:scrap同质食物在制损耗，分别1/1/1/0份，不代表原料盘亏 |
| 包材 | 条件必需 | provided | finance:packaging按批准耗用，不由销售额估算 |
| 人工 | 必需 | provided | finance:kitchen_labor和shared:shared_kitchen_labor为不同人员群，不重复 |
| 房租 | 条件必需 | provided | finance:rent本期费用，按批准期间口径 |
| 水电 | 条件必需 | provided | shared:utilities，多门店渠道份数动因 |
| 设备折旧 | 条件必需 | provided | shared:depreciation资产基数÷12 |
| 外卖佣金 | 条件必需 | provided | finance:commission与settlements同渠道settlement对象精确勾稽；只作为费用一次 |
| 支付手续费 | 条件必需 | provided/zero | 堂食2+1元；外卖两渠道在settlements明确0，不按缺行推零 |
| 促销优惠 | 条件必需 | provided/zero | merchant_discount减收入；platform_discount由平台补偿，不再抵收入或加成本 |
| 退款/退单 | 条件必需 | provided/zero | settlements:refund关联原gross，events:return关联原sale；两个来源分别验证 |
| 总部共享费用 | 条件必需 | provided | shared:head_office按各门店渠道events.csv两菜品批准投入份数合计分摊至对应period对象。金额4500分，权重20:30:25:15，生产库存不含该费用 |
| 实结回款 | 必需 | provided | gross−商家优惠−退款−佣金−支付费=receipt，差额独立检查；stock零售收入另有原始结算行 |
| 订单级结果 | 可选 | missing | 本样例仅门店/堂食与外卖渠道汇总凭证，逐订单POS未验证 |
| 税务/加盟/团餐中央厨房完整账 | 不适用 | not_applicable | 不纳入本轮范围 |

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
