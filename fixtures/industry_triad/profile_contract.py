"""Versioned industry configuration; no industry branches in the costing core."""
import hashlib
import json
from decimal import Decimal

LABELS = {'pharma': '制药', 'restaurant': '餐饮', 'medical_devices': '医疗器械'}

NOTICES = {
    'pharma': [
        ('approved_homogeneous_basis', '仅按合成企业批准的同质等价投入单位估值；不是配方、工艺或自动等价产量判断。'),
        ('capitalization_policy_external', '研发全部按本样例批准的期间费用处理；真实资本化、税务、药品监管判断需企业批准。'),
        ('production_material_return_scope', '原料采购入库、批次领料与关联原领料的退料均有凭证。批次在制基于批准同质投入份额，异质在制重估未覆盖。')],
    'restaurant': [
        ('native_pos_unimplemented', '菜品成本接收批准的厨房领料与退料凭证及抽象同质份数。原生POS与配方净料率自动转换未实现。'),
        ('procurement_inventory_boundary', 'raw_materials核对厨房原料采购、菜品领料和退料。stock核对独立即售商品采购、跨店调拨、盘点损耗和销售退回。厨房原料跨店调拨及实际盘点尚未纳入样例。'),
        ('channel_fee_not_inventory', '门店×渠道×菜品对象归集已耗用食材及厨房成本。settlement对象单列必需的渠道费用。总部费用按批准投入份数分至各门店渠道的期间中心。'),
        ('order_grain_unverified', '结算按门店/渠道汇总凭证；未验证逐订单POS追溯。')],
    'medical_devices': [
        ('recall_basis_missing', '未提供经批准的召回或保修准备估计规则，不自动计提准备金；实际保修支出单列。'),
        ('serial_evidence_only', '仅保留抽象批次/文本凭证，序列号逐台追溯未实现。'),
        ('disposition_scope', 'dispositions以合成批准单位身份登记返工/报废/退回并验证互斥及原记录引用；不是企业真实序列号或物理身份独立核验。'),
        ('registration_policy_external', '注册/检测费用按合成批准口径当期确认；真实资本化、监管和税务需另行批准。')],
}


def membership(scope, group_id, members):
    payload = dict(ledger_scope=scope, group_id=group_id,
                   members=sorted([m['input_name'], m['object_id']] for m in members))
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def required_cost_elements(industry, obj):
    """Approved mandatory categories for each role in the declared fixture."""
    name = obj['id']
    classification = obj['classification']
    if industry == 'pharma':
        if classification == 'production':
            return ['materials', 'packaging', 'labor', 'outsourcing', 'quality', 'release',
                    'utilities', 'depreciation', 'licence_amortization']
        return ['research_expense'] if name.startswith('period-rd') else ['warehouse_logistics']
    if industry == 'medical_devices':
        if classification == 'production':
            mandatory = ['parts', 'assembly_labor', 'outsourcing', 'quality', 'depreciation', 'utilities']
            return mandatory + (['rework'] if name.startswith('device-batch-a') else ['packaging'])
        if name.startswith('period-registration'):
            return ['registration_testing']
        if name.startswith('period-logistics'):
            return ['warehouse_logistics']
        return ['actual_warranty'] if name.startswith('service-warranty') else ['after_sales']
    if classification == 'production':
        return ['ingredients_consumed', 'kitchen_labor', 'packaging', 'utilities', 'depreciation', 'shared_kitchen_labor']
    if classification == 'period':
        return ['rent'] if name.startswith('period-rent') else ['head_office']
    if obj['input_name'] == 'stock':
        return ['resale_cogs', 'inventory_shrink']
    return ['commission'] if '-delivery-' in name else ['payment_fee']


def make_schema(bundle):
    industry = bundle['industry']; scope = bundle['scope']; desc = bundle['descriptor']
    def branch(tags, shared=False):
        rules = []; maps = {}
        for tag, meta in tags.items():
            rule = dict(id=tag, op='monthly_straight_line' if 'months' in meta else 'recognize',
                        source_tag=tag, source_kind=meta['kind'], posting_pool='approved-incurred-scope')
            if 'months' in meta: rule.update(start='source_origin', months=meta['months'])
            rules.append(rule)
            spec = dict(object_kind='cost_center' if shared else 'product', record_type='pool' if shared else 'entry',
                        cost_element=meta['element'], behavior='fixed' if shared else 'unknown', cost_type='indirect' if shared else 'direct')
            if shared:
                spec.update(object_id='shared-' + tag, pool_id=tag)
            else:
                # All declared targets have product kind to preserve consistent
                # metadata with v2 dynamic source mappings. Financial class is
                # explicit in business_views, not inferred from object_kind.
                spec['object_id_from'] = 'target_object_id'
            maps[tag] = spec
        cfg = dict(version=2, name=LABELS[industry] + '抽象财务原始来源',
                   context=scope, rules=rules, outputs=list(tags), excluded_tags={'cash_only': '结算/支付备查，不作为第二笔成本'},
                   posting_map=maps, ledger_basis='recognized_expenses_incurred')
        if not shared:
            cfg['target_objects'] = [dict(object_id=o['id'], object_kind='product') for o in desc['objects'] if o['input_name'] == 'finance']
        return cfg

    rules = [dict(rule_id='to-bridge-' + tag, pool_id='shared:' + tag, target_ids=['bridge'], method='direct', stage=1)
             for tag in desc['shared_tags'] if tag != 'head_office']
    rules.append(dict(rule_id='bridge-to-cost-objects', pool_id='global:bridge', target_ids=list(desc['allocation_weights']),
                      weights=desc['allocation_weights'], method='driver', stage=2))
    if desc['period_weights']:
        rules.append(dict(rule_id='head-office-by-approved-input-portions', pool_id='shared:head_office',
                          method='quantity', stage=1, driver_name='门店渠道批准投入份数（events.csv start 合计）',
                          driver_unit='approved_unit', target_ids=list(desc['period_weights']), weights=desc['period_weights']))
    rollups = []; budget_rows = []
    for ix, obj in enumerate(desc['objects'], 1):
        group_id = f'object-{ix:03}'
        members = [dict(input_name=obj['input_name'], object_id=obj['id'])]
        rollups.append(dict(id=group_id, label=obj['id'], members=members))
        cents = desc['budgets'][obj['id']]
        budget_rows.append(dict(group_id=group_id, amount=format(Decimal(cents) / 100, '.2f'),
                                status='budgeted' if cents else 'evidenced_zero',
                                membership_sha256=membership(scope, group_id, members),
                                source_id='synthetic-approved-budget-v1:' + obj['id']))
    envelope = dict(version=4, scope=scope,
                    inputs=dict(finance=dict(file='finance', raw_recognition=branch(desc['finance_tags'])),
                                shared=dict(file='shared', raw_recognition=branch(desc['shared_tags'], True))),
                    objects=[dict(object_id='bridge', name='共享制造费用归集中心', kind='cost_center', currency='CNY')],
                    incoming_pools=[dict(pool_id='global:bridge', owner_id='bridge', element_id='overhead')],
                    allocation_rules=rules,
                    object_rollups=rollups, object_budgets=dict(version=1, **scope, rows=budget_rows))
    if desc['stock_streams']:
        inventory = dict(operation='moving_weighted_average',
                         policy='line_half_up_minor_units_with_cumulative_original_issue_returns',
                         context=dict(**scope, cutoff_at='2026-11-30T23:59:59Z'),
                         chronology='sort_by_utc_timestamp_then_sequence',
                         transfer_policy='closed_same_period_pairs_v1', transfer_history=[], streams=desc['stock_streams'])
        envelope['inputs']['stock'] = dict(file='stock', raw_inventory=inventory)
    if desc['raw_material_streams']:
        inventory = dict(operation='moving_weighted_average',
                         policy='line_half_up_minor_units_with_cumulative_original_issue_returns',
                         context=dict(**scope, cutoff_at='2026-11-30T23:59:59Z'),
                         chronology='sort_by_utc_timestamp_then_sequence',
                         production_policy='same_period_approved_batch_consumption_v1',
                         streams=desc['raw_material_streams'])
        envelope['inputs']['raw_materials'] = dict(file='raw_materials', raw_inventory=inventory)
    envelope['source_hashes'] = {name: hashlib.sha256(text.encode('utf-8')).hexdigest() for name, text in bundle['files'].items()}
    business = dict(version=1, policy_source='synthetic approved homogeneous equivalent-unit financial policy v1; 2026-11',
                    classifications={o['id']: o['classification'] for o in desc['objects']},
                    required_costs={o['id']: required_cost_elements(industry, o) for o in desc['objects']},
                    lifecycle=dict(file='events', columns={k: k for k in ('id', 'object_id', 'op', 'quantity', 'unit', 'ref')},
                                   units={o['id']: o['unit'] for o in desc['objects'] if o['classification'] == 'production'}),
                    notices=[dict(code='declared_scope_only', message='合成验收范围内的已确认发生成本；不代表整个行业会计或企业全部交易已完整采集。')]
                            + [dict(code=c, message=m) for c, m in NOTICES[industry]])
    if 'settlements' in bundle['files']:
        business['settlement'] = dict(file='settlements', columns={k: k for k in ('id', 'object_id', 'op', 'amount', 'ref')},
                                      commission_element='commission', payment_fee_element='payment_fee',
                                      merchant_discount_treatment='revenue_reduction', platform_discount_treatment='platform_compensated',
                                      expected_objects=desc['settlement_objects'])
    if 'dispositions' in bundle['files']:
        business['dispositions'] = dict(file='dispositions',
                                       columns={k: k for k in ('id', 'object_id', 'item_id', 'action', 'ref')},
                                       identity_basis='synthetic approved unit identity registry v1',
                                       action_tags={'rework': 'rework'})
    identity = hashlib.sha256((industry + bundle['expected']['variant']).encode()).hexdigest()[:16]
    return dict(dataset_id='csv-' + identity, plugin_id='csv-generic', plugin_version='industry-triad-1.0.0', industry=LABELS[industry],
                label=LABELS[industry] + '合成范围 v1 ' + bundle['expected']['variant'],
                period=scope['period'], currency='CNY', scenario='actual', raw_composite=envelope, business_views=business)


COVERAGE = {
    'pharma': [
        ('原辅包材', '必需', 'provided', 'raw_materials原辅料采购和可追溯领退料进入批次materials。finance:packaging单列本期包材耗用'),
        ('直接人工', '必需', 'provided', 'finance:labor为accrual，支付备查cash_only不重复确认'),
        ('外协', '条件必需', 'provided', 'finance:outsourcing'),
        ('检验与放行', '条件必需', 'provided', 'finance:quality/release，两个不同成本组'),
        ('研发及试验', '条件必需', 'provided', 'finance:research_expense，批准全部为期间费用，无自动资本化'),
        ('公用工程', '条件必需', 'provided', 'shared:utilities→bridge→批次，2:1批准动因'),
        ('设备折旧', '条件必需', 'provided', 'shared:depreciation原资产基数÷12，仅本期摊销进入成本'),
        ('许可与预付摊销', '条件必需', 'provided', 'shared:licence_amortization预付基数÷12，原值不整笔费用化'),
        ('间接制造费用', '必需', 'provided', '共享源多阶段归集；原成本组不因bridge改名'),
        ('批次报废', '条件必需', 'provided', 'events:scrap只从在制扣，样例批次a报废10，b显式无该事件且量桥可核'),
        ('成品退回/销售冲销', '条件必需', 'provided', 'events:return关联原sale，回补成品，净COGS冲回，不新记第二笔退回费用'),
        ('生产投料退回', '条件必需', 'provided', 'raw_materials:production_return绑定原领料并回补库存；本期采购额等于批次净投料加原料期末余额'),
        ('在制/成品/销售成本', '必需', 'provided', 'events同质投入120/90、完工100/80，财务发生成本分区守恒'),
        ('批准投入/合格/放行量', '必需', 'provided', '抽象approved_unit，投入及完工来自events原始行；无配方或实验操作'),
        ('仓储物流', '条件必需', 'provided', 'finance:warehouse_logistics，按合成企业批准为期间支出'),
        ('结算/现金流', '可选', 'provided', 'cash_only为排除的支付备查；本行业样例无收入结算模型'),
        ('税务/GMP/自动资本化', '不适用', 'not_applicable', '本轮抽象财务验证不形成监管合规或税务判断')],
    'restaurant': [
        ('采购及供应商结算', '条件必需', 'provided/missing', 'raw_materials厨房与stock即售商品分别保存采购入库原值。供应商付款只保留现金备查'),
        ('配方/净料/逐单领用', '条件必需', 'missing', 'native_pos_unimplemented：不从POS或配方自动推导可靠领用'),
        ('成本对象与批准食材耗用', '必需', 'provided', 'raw_materials原始入库、领料和退料生成ingredients_consumed，两个门店×堂食/外卖×rice（米饭类）/noodles（面食类）共八对象'),
        ('领用/调拨/盘点', '条件必需', 'provided/missing', '厨房领料及关联原领料退库有来源。stock即售商品跨店闭环调拨10元、盘点损耗4元。厨房原料跨店调拨/盘点未纳入样例'),
        ('报损', '条件必需', 'provided', 'events:scrap同质食物在制损耗，分别1/1/1/0份，不代表原料盘亏'),
        ('包材', '条件必需', 'provided', 'finance:packaging按批准耗用，不由销售额估算'),
        ('人工', '必需', 'provided', 'finance:kitchen_labor和shared:shared_kitchen_labor为不同人员群，不重复'),
        ('房租', '条件必需', 'provided', 'finance:rent本期费用，按批准期间口径'),
        ('水电', '条件必需', 'provided', 'shared:utilities，多门店渠道份数动因'),
        ('设备折旧', '条件必需', 'provided', 'shared:depreciation资产基数÷12'),
        ('外卖佣金', '条件必需', 'provided', 'finance:commission与settlements同渠道settlement对象精确勾稽；只作为费用一次'),
        ('支付手续费', '条件必需', 'provided/zero', '堂食2+1元；外卖两渠道在settlements明确0，不按缺行推零'),
        ('促销优惠', '条件必需', 'provided/zero', 'merchant_discount减收入；platform_discount由平台补偿，不再抵收入或加成本'),
        ('退款/退单', '条件必需', 'provided/zero', 'settlements:refund关联原gross，events:return关联原sale；两个来源分别验证'),
        ('总部共享费用', '条件必需', 'provided', 'shared:head_office按各门店渠道events.csv两菜品批准投入份数合计分摊至对应period对象。金额4500分，权重20:30:25:15，生产库存不含该费用'),
        ('实结回款', '必需', 'provided', 'gross−商家优惠−退款−佣金−支付费=receipt，差额独立检查；stock零售收入另有原始结算行'),
        ('订单级结果', '可选', 'missing', '本样例仅门店/堂食与外卖渠道汇总凭证，逐订单POS未验证'),
        ('税务/加盟/团餐中央厨房完整账', '不适用', 'not_applicable', '不纳入本轮范围')],
    'medical_devices': [
        ('零部件及外购', '必需', 'provided', 'raw_materials按原采购、领投料和关联原领料的退料估值，净投料进入批次parts'),
        ('装配人工', '必需', 'provided', 'finance:assembly_labor'),
        ('委外', '条件必需', 'provided', 'finance:outsourcing'),
        ('检测质检', '条件必需', 'provided', 'finance:quality为生产质检；registration_testing另作期间归属'),
        ('返工', '条件必需', 'provided', 'finance:rework=60；dispositions批准单位身份逐行引用原返工凭证，与报废/退回全局互斥'),
        ('包装', '条件必需', 'provided', 'finance:packaging，批次b单列'),
        ('报废', '条件必需', 'provided', 'events:scrap两个批次10/5，同一事件只分出一次损失'),
        ('库存与在制', '必需', 'provided', 'events双批次同质量桥，资产+损益=发生成本；不自动推等价完工程度'),
        ('退货冲回', '条件必需', 'provided', 'events:return关联原sale，超过原量失败，回补库存并冲回净COGS'),
        ('仓储物流', '条件必需', 'provided', 'finance:warehouse_logistics=60，批准期间费用'),
        ('设备折旧', '条件必需', 'provided', 'shared:depreciation资产基数÷12'),
        ('公用/间接制造费', '条件必需', 'provided', 'shared:utilities，两阶段1:1分摊'),
        ('注册或检测支出期间归属', '条件必需', 'provided', 'finance:registration_testing=120，仅体现合成企业批准口径'),
        ('实际保修', '条件必需', 'provided', 'finance:actual_warranty=80，service对象，与准备金分开'),
        ('正常售后', '条件必需', 'provided', 'finance:after_sales=40，独立service对象'),
        ('保修/召回准备估计依据', '条件必需', 'missing', 'recall_basis_missing提示；无批准依据不计提，也不把实际支出当准备金'),
        ('序列号逐台追溯', '可选', 'missing', 'serial_evidence_only：批次和文本凭证保留，不形成逐台追踪'),
        ('批准单位处置防重', '必需', 'provided', 'dispositions单位身份为合成批准registry，返工/报废/退回互斥；不声称真实企业序列号逐台独立核验'),
        ('现金/回款', '可选', 'provided', 'cash_only排除的支付备查；无收入结算模型'),
        ('注册监管/税务/召回实施判断', '不适用', 'not_applicable', '不形成合规结论或医疗/生产操作建议')]
}


def coverage_dictionary(industry):
    lines = [f'# {LABELS[industry]}字段与覆盖字典 v1', '',
             '所有数据为独立生成的抽象财务合成资料，期间2026-11、账簿SYNTH_' + industry.upper() + '、CNY两位货币。', '',
             '| 项目 | 要求 | 样例状态 | 来源/口径与边界 |', '|---|---|---|---|']
    lines.extend('| ' + ' | '.join(row) + ' |' for row in COVERAGE[industry])
    lines.extend(['', '## 文件字段', '',
                  '| 文件 | 字段 | 校验与用途 |', '|---|---|---|',
                  '| finance/shared.csv | id/economic_key | 唯一凭证与经济事项；重复、别名冲突应拒绝 |',
                  '| finance/shared.csv | book/period/currency | 必需，须与共同scope完全一致 |',
                  '| finance/shared.csv | kind/status/amount | 原始费用、应计、资产基数、预付基数或现金备查；金额十进制CNY两位，missing不能填0 |',
                  '| finance/shared.csv | tag/target_object_id | 版本化规则映射至成本组与对象，输入不含预计算总成本 |',
                  '| finance/shared.csv | evidence/origin_period | 合成凭证来源说明；摊销起期明确2026-01 |',
                  '| events.csv | id/object_id/op/quantity/unit/ref | 原始抽象投入/完工/销售/退回/报废数量；approved_unit不可与其他单位混用；return必须关联原sale |',
                  '| stock.csv（餐饮） | warehouse/item/uom/at/sequence/kind/quantity/receipt_value/original_issue_id/transfer_id | 独立即售包装商品pack单位；采购值不是当期费用，闭环调拨不是新增成本，退货须关联原销售 |',
                  '| raw_materials.csv | warehouse/item/uom/kind/quantity/receipt_value/production_object_id/economic_key/original_issue_id | 采购入库以approved_input_unit计量。生产/厨房领料扣减库存，关联原领料的退料回补库存。净耗用进入批次或菜品，原料物量与完工等价份额各有独立单位 |',
                  '| dispositions.csv（器械） | id/object_id/item_id/action/ref | 合成批准单位身份与返工/报废/退回互斥，ref分别关联原财务或生命周期事件；不是实际序列号追踪 |',
                  '| settlements.csv（餐饮） | id/object_id/op/amount/ref | gross为原记录，其余优惠/退款/佣金/手续费/回款均关联gross |',
                  '| rules.json | raw_composite.version=4 | 同一编译器多文件接入、共同账簿、跨源有序分摊；中间归集不能增加成本 |',
                  '| rules.json | business_views | 明示production/period/service，投入同质估值，未知/不适用由notices披露 |',
                  '| rules.json | business_views.required_costs | 逐对象声明关键成本组。有条件项按合成对象适用性声明，来源缺项不按零 |',
                  '| rules.json | object_budgets | 合成批准预算，成员及期间哈希绑定；不是实际企业预算 |',
                  '| expected.json | oracle | 独立整数分/有理数分摊与物量恒等式，不导入待测引擎 |', '',
                  'zero表示有明确零凭证/结算行，missing表示缺证据或未实现，not_applicable只对当前声明口径成立。',
                  'approved_unit是无生产含义的抽象同质等价单位；完工合格单位成本=分配至完工部分成本÷有来源的完工量。',
                  '库存物量价值按总投入的同质比例分配，保留在制、成品、净销售成本、报废四分区；不适用于不同完工程度或异质批次自动估值。'])
    return '\n'.join(lines) + '\n'


def instructions(industry):
    extra_files = dict(pharma='、raw_materials.csv', restaurant='、raw_materials.csv、settlements.csv、stock.csv',
                       medical_devices='、raw_materials.csv、dispositions.csv')
    extra_keys = dict(pharma='/raw_materials', restaurant='/raw_materials/settlements/stock',
                      medical_devices='/raw_materials/dispositions')
    return f'''# {LABELS[industry]}隔离合成验收操作 v1

本文件只说明本次声明范围，完整字段及未覆盖项见field_dictionary.md。仅用合成账号和独立验收端口。正式8765不变更。

1. 以同一候选源码运行 `python -B fixtures/industry_triad/generate.py` 复现三个最小样例；加 `--large` 生成各自large子目录的大样本。
2. 同一登录会话上传本目录finance.csv、shared.csv、events.csv{extra_files[industry]}及rules.json。文件键分别为finance/shared/events{extra_keys[industry]}。CSV保留原始凭证和数量。
3. 运行2026-11 actual，核对发生成本、成本组、对象、预算差异、资产/损益分区、缺项notice和原始行。中间bridge及shared中心余额应清零。
4. 保存analysis_id/revision，展开财务与事件来源，重开同一修订。JSON/Markdown/Word/PDF应包含同一身份、金额和规则边界。
5. 对照expected.json独立预期；运行 `python -B -m unittest discover -s tests -p test_industry_triad.py -v`。真实HTTP和Edge操作证据另记，单元测试不能替代。
6. 大样本与失败样例分别留证；同会话顺序制药→餐饮→医疗器械→制药，重开最早制药版本核对行业/金额/来源/导出。

大样本seed={20261114073}，六组不同物量倍率及单位成本倍率、40份变额原始凭证；包含不同对象、成本组、现金排除、退回和损耗，不是复制同一行。预期仅用整数分与Fraction，完全不导入成本引擎。

财务发生成本不是当期利润成本：production部分拆在制/成品/净COGS/报废；period直接期间费用；service为实际服务费用。现金备查不计成本。行业完整性始终受声明边界限制，不宣称全行业已覆盖。
'''
