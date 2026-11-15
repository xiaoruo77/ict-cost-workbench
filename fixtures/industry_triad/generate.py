"""Reproducible abstract financial sources and independent integer-cent oracle.

No import of the costing implementation is allowed here. Small fixtures are
one declared scope; --large uses six economically different groups and forty
different voucher fragments per monetary component, rather than cloned rows.
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from decimal import Decimal as D
from fractions import Fraction
import csv
import io
import json
from pathlib import Path
import random

SEED = 20261114073
PERIOD = '2026-11'
INDUSTRIES = ('pharma', 'restaurant', 'medical_devices')
FIN_FIELDS = 'id book period currency kind status amount economic_key evidence tag target_object_id origin_period'.split()
EVENT_FIELDS = 'id book period currency object_id op quantity unit ref'.split()
SETTLEMENT_FIELDS = 'id book period currency object_id op amount ref'.split()
STOCK_FIELDS = 'id source_id book warehouse item uom currency at sequence kind quantity evidence receipt_value original_issue_id restock_status transfer_id'.split()
MATERIAL_FIELDS = [*STOCK_FIELDS[:-1], 'production_object_id', 'economic_key']
DISPOSITION_FIELDS = 'id book period currency object_id item_id action ref'.split()


def money(cents):
    return format(D(cents) / 100, '.2f')


def csv_text(rows, fields):
    buf = io.StringIO(newline='')
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def partition(total, weights):
    """Independent rational largest remainder, in integer cents."""
    values = {k: Fraction(total * v, sum(weights.values())) for k, v in weights.items()}
    parts = {k: int(v) for k, v in values.items()}
    for k in sorted(values, key=lambda x: (-(values[x] - parts[x]), x))[:total - sum(parts.values())]:
        parts[k] += 1
    assert sum(parts.values()) == total
    return parts


def fragments(total, n, rng):
    if n == 1:
        return [total]
    assert total >= n
    cuts = sorted(rng.sample(range(1, total), n - 1))
    out = [b - a for a, b in zip([0] + cuts, cuts + [total])]
    assert min(out) > 0 and sum(out) == total
    return out


def build(industry, large=False):
    if industry not in INDUSTRIES:
        raise ValueError('unknown fixture industry')
    rng = random.Random(SEED + INDUSTRIES.index(industry) * 100 + int(large))
    book = 'SYNTH_' + industry.upper()
    scope = dict(book=book, period=PERIOD, currency='CNY')
    finance, shared, events, settlements, stock, dispositions = [], [], [], [], [], []
    stock_streams = []
    stock_totals = defaultdict(int)
    raw_materials = []
    raw_material_streams = []
    raw_material_expected = {}
    objects, finance_tags, shared_tags = {}, {}, {}
    direct, shared_expected = defaultdict(lambda: defaultdict(int)), defaultdict(int)
    allocations, weights, quantities, budgets = {}, {}, {}, {}
    period_weights = {}
    menu_dimensions = {}
    settlement_want, excluded_cash = {}, 0
    groups = [(1, 1)] if not large else [(1, 1), (3, 2), (2, 3), (5, 1), (4, 4), (6, 2)]
    if large and industry == 'restaurant':
        # Keep each object budget independently pinned within the 96-group
        # contract. The last cohort replaces two prior cohorts (16 + 12).
        groups = [(1, 1), (3, 2), (2, 3), (5, 1), (7, 4)]
    repeats = 40 if large else 1

    def oid(base, g):
        return base if not large else base + '-g' + str(g + 1).zfill(2)

    def add_object(identity, classification, unit=''):
        objects[identity] = dict(id=identity, classification=classification, unit=unit, input_name='finance')

    def voucher(target, tag, cents, classification, kind='expense'):
        finance_tags[tag] = dict(kind=kind, element=tag)
        for piece, amount in enumerate(fragments(cents, min(repeats, cents), rng), 1):
            identity = f'FIN-{target}-{tag}-{piece:03}'
            finance.append(dict(**scope, id=identity, kind=kind, status='data', amount=money(amount),
                                economic_key=identity, evidence=f'abstract synthetic voucher {identity}; policy-v1',
                                tag=tag, target_object_id=target, origin_period=''))
        direct[target][tag] += cents
        add_object(target, classification, 'approved_unit' if classification == 'production' else '')

    def lifecycle(target, start, complete, sale, returned=0, scrap=0):
        quantities[target] = dict(start=start, complete=complete, sale=sale, returned=returned, scrap=scrap)
        original = 'EV-' + target + '-sale'
        for op, qty, ref in [('start', start, ''), ('complete', complete, ''), ('sale', sale, ''),
                             ('return', returned, original), ('scrap', scrap, '')]:
            if not qty and op in {'return', 'scrap'}:
                continue
            identity = original if op == 'sale' else 'EV-' + target + '-' + op
            events.append(dict(**scope, id=identity, object_id=target, op=op, quantity=str(qty), unit='approved_unit', ref=ref))

    def settlement(target, gross, merchant, platform, refund, commission, fee):
        net = gross - merchant - refund
        values = dict(gross=gross, merchant_discount=merchant, platform_discount=platform,
                      refund=refund, commission=commission, payment_fee=fee, receipt=net - commission - fee)
        identity = 'SET-' + target + '-gross'
        for op, amount in values.items():
            # Explicit zero is evidence, distinct from missing source rows.
            settlements.append(dict(**scope, id='SET-' + target + '-' + op, object_id=target,
                                    op=op, amount=money(amount), ref='' if op == 'gross' else identity))
        settlement_want[target] = {**values, 'net_revenue': net,
                                   'expected_receipt': net - commission - fee, 'receipt_difference': 0}

    def material_inputs(target, element, net_quantity, quantity_multiplier, unit_price_cents, issue_excess):
        raw_unit = 'approved_input_unit'
        batch_scope = dict(warehouse='RM-' + target, item='ABSTRACT-' + element.upper(), uom=raw_unit)
        identity = dict(book=book, currency='CNY', **batch_scope)
        opening = dict(**identity, as_of='2026-10-31T23:59:59Z', quantity='0', value='0.00',
                       status='evidenced_zero', evidence='approved abstract opening materials count: zero',
                       issues=[], seen_event_ids=[], seen_source_ids=[])
        posting = dict(object_id=target, object_kind='product', cost_type='direct', behavior='unknown')
        raw_material_streams.append(dict(scope=batch_scope, opening=opening,
            posting_map=dict(cogs=dict(posting, cost_element='raw_materials_resale'),
                             shrink=dict(posting, cost_element='raw_materials_shrink')),
            production_posting_map={target: dict(object_kind='product', cost_element=element,
                                                cost_type='direct', behavior='unknown')}))
        closing_quantity = (issue_excess + 12) * quantity_multiplier
        returned_quantity = issue_excess * quantity_multiplier
        received_quantity = net_quantity + closing_quantity
        issued_quantity = net_quantity + returned_quantity

        def movement(token, kind, quantity, day, sequence, **extra):
            event_id = f'RAW-{target}-{token}'
            raw_materials.append(dict(**identity, id=event_id, source_id='DOC-' + event_id,
                                      at=f'2026-11-{day:02d}T08:00:00Z', sequence=str(sequence), kind=kind,
                                      quantity=str(quantity), evidence='approved abstract materials voucher ' + event_id,
                                      **extra))
            return event_id

        for index, quantity in enumerate(fragments(received_quantity, repeats, rng), 1):
            movement(f'purchase-{index:03}', 'receipt', quantity, 1, index,
                     receipt_value=money(quantity * unit_price_cents))
        issue_ref = movement('issue', 'production_issue', issued_quantity, 2, 1,
                             production_object_id=target, economic_key=f'{target}-material-issue')
        movement('return', 'production_return', returned_quantity, 3, 1,
                 production_object_id=target, economic_key=f'{target}-material-return',
                 original_issue_id=issue_ref, restock_status='restockable')

        available = received_quantity * unit_price_cents
        consumption = net_quantity * unit_price_cents
        closing = closing_quantity * unit_price_cents
        assert available == consumption + closing
        direct[target][element] += consumption
        raw_material_expected[target] = dict(net_quantity=str(net_quantity), closing_quantity=str(closing_quantity),
                                             available_value=money(available), net_production_consumption=money(consumption),
                                             closing_value=money(closing))

    for g, (qmult, rate) in enumerate(groups):
        mult = qmult * rate
        if industry == 'pharma':
            for native, amounts, counts, weight, budget in [
                ('batch-a', dict(materials=60000, labor=18000, outsourcing=10000, quality=6000, release=4000, packaging=2000), (120, 100, 60, 5, 10), 2, 115000),
                ('batch-b', dict(materials=45000, labor=15000, outsourcing=8000, quality=6000, release=4000, packaging=2000), (90, 80, 50, 0, 0), 1, 100000)]:
                target = oid(native, g)
                material_inputs(target, 'materials', amounts['materials'] // 100 * qmult, qmult, 100 * rate,
                                issue_excess=25 if native == 'batch-a' else 15)
                for tag, cents in amounts.items():
                    if tag != 'materials':
                        voucher(target, tag, cents * mult, 'production', 'accrual' if tag == 'labor' else 'expense')
                lifecycle(target, *(x * qmult for x in counts))
                weights[target] = weight * mult
                budgets[target] = budget * mult
            for native, tag, cents, budget in [('period-rd', 'research_expense', 24000, 25000), ('period-logistics', 'warehouse_logistics', 6000, 5000)]:
                target = oid(native, g); voucher(target, tag, cents * mult, 'period'); budgets[target] = budget * mult
            shared_amounts = dict(utilities=18000, depreciation=9000, licence_amortization=3000)
        elif industry == 'restaurant':
            for store, channel_name, rice, noodles, gross, merchant, platform, refund, commission, fee in [
                ('store-1', 'dine', (11, 10, 9, 1, 1), (9, 8, 8, 0, 0), 40000, 2000, 0, 2000, 0, 200),
                ('store-1', 'delivery', (17, 16, 14, 1, 0), (13, 12, 11, 1, 1), 60000, 3000, 4000, 6000, 5100, 0),
                ('store-2', 'dine', (14, 13, 12, 0, 1), (11, 11, 10, 0, 0), 50000, 0, 0, 0, 0, 100),
                ('store-2', 'delivery', (8, 8, 7, 1, 0), (7, 6, 5, 0, 0), 30000, 1500, 2000, 3000, 2550, 0)]:
                native = f'{store}-{channel_name}'
                channel = oid(native + '-settlement', g)
                for dish, counts, mix_offset in [('rice', rice, rice[0] * 20), ('noodles', noodles, -rice[0] * 20)]:
                    food = oid(native + '-food-' + dish, g)
                    start = counts[0]
                    costs = dict(ingredients_consumed=300 * start + mix_offset,
                                 kitchen_labor=100 * start - mix_offset, packaging=100 * start)
                    material_inputs(food, 'ingredients_consumed', costs['ingredients_consumed'] // 20 * qmult,
                                    qmult, 20 * rate, issue_excess=3 if dish == 'rice' else 2)
                    for tag, amount in costs.items():
                        if tag != 'ingredients_consumed':
                            voucher(food, tag, amount * mult, 'production')
                    lifecycle(food, *(x * qmult for x in counts))
                    weights[food] = start * mult
                    budgets[food] = start * 650 * mult
                    menu_dimensions[food] = dict(store=oid(store, g), channel=channel_name, dish=dish)

                period_center = oid(native + '-period', g)
                add_object(period_center, 'period')
                # The approved HQ driver is sourced from the two dish inputs
                # in events.csv. Budget remains a separately declared plan.
                period_weights[period_center] = (rice[0] + noodles[0]) * qmult
                budgets[period_center] = (rice[0] + noodles[0]) * 50 * mult
                # Fee objects remain service expense, never inventory/WIP.
                add_object(channel, 'service')
                if commission:
                    voucher(channel, 'commission', commission * mult, 'service')
                if fee:
                    voucher(channel, 'payment_fee', fee * mult, 'service')
                budgets[channel] = (commission + fee) * mult
                settlement(channel, *(x * mult for x in (gross, merchant, platform, refund, commission, fee)))
            rent_center = oid('period-rent', g)
            voucher(rent_center, 'rent', 6000 * mult, 'period')
            budgets[rent_center] = 6000 * mult
            # Distinct ready-to-sell packaged goods, not kitchen production
            # issues. Reuse the existing inventory/closed-transfer compiler.
            sites = [oid('store-1-resale', g), oid('store-2-resale', g)]
            for target in sites:
                add_object(target, 'service'); objects[target]['input_name'] = 'stock'
                direct[target]['resale_cogs'] = 1600 * mult
                direct[target]['inventory_shrink'] = 200 * mult
                budgets[target] = 2000 * mult
                attrs = dict(book=book, currency='CNY', uom='pack', item='ABSTRACT_PACKAGED_GOODS', warehouse=target)
                posting = dict(object_id=target, object_kind='product', behavior='variable', cost_type='direct')
                stock_streams.append(dict(scope={k: attrs[k] for k in ('warehouse', 'item', 'uom')},
                    opening=dict(**attrs, as_of='2026-10-31T23:59:59Z', quantity='0', value='0.00',
                                 status='evidenced_zero', evidence='synthetic approved opening count: zero',
                                 issues=[], seen_event_ids=[], seen_source_ids=[]),
                    posting_map=dict(cogs=dict(posting, cost_element='resale_cogs'),
                                     shrink=dict(posting, cost_element='inventory_shrink'))))
            transfer = 'TRANSFER-' + str(g + 1)
            moves = [(0, 'receipt', 20, 1, {'receipt_value': money(4000 * mult)}),
                     (1, 'receipt', 10, 2, {'receipt_value': money(2000 * mult)}),
                     (0, 'transfer_out', 5, 3, {'transfer_id': transfer}),
                     (1, 'transfer_in', 5, 4, {'transfer_id': transfer}),
                     (0, 'sale_issue', 10, 5, {}),
                     (0, 'sale_return', 2, 6, {'original_issue_id': f'STOCK-{g + 1}-5', 'restock_status': 'restockable'}),
                     (0, 'shrink', 1, 7, {}),
                     (1, 'sale_issue', 8, 8, {}),
                     (1, 'shrink', 1, 9, {})]
            for site, kind, qty, day, special in moves:
                identity = f'STOCK-{g + 1}-{day}'
                stock.append(dict(id=identity, source_id='DOC-' + identity, book=book, warehouse=sites[site],
                                  item='ABSTRACT_PACKAGED_GOODS', uom='pack', currency='CNY',
                                  at=f'2026-11-{day:02}T10:00:00Z', sequence=str(g + 1), kind=kind,
                                  quantity=str(qty * qmult), evidence='synthetic purchase/count/closed-transfer voucher ' + identity,
                                  **special))
            settlement(sites[0], 4000 * mult, 0, 0, 800 * mult, 0, 0)
            settlement(sites[1], 3200 * mult, 0, 0, 0, 0, 0)
            for key, cents in dict(available_value=6000, net_cogs=3200, shrink=400, closing_value=2400,
                                   closed_transfer_value=1000).items():
                stock_totals[key] += cents * mult
            shared_amounts = dict(utilities=3600, depreciation=1800, shared_kitchen_labor=3600, head_office=4500)
        else:
            for native, amounts, counts, budget in [
                ('device-batch-a', dict(parts=60000, assembly_labor=16000, outsourcing=10000, quality=8000, rework=6000), (110, 90, 60, 5, 10), 100000),
                ('device-batch-b', dict(parts=50000, assembly_labor=10000, outsourcing=8000, quality=8000, packaging=4000), (90, 80, 40, 2, 5), 95000)]:
                target = oid(native, g)
                material_inputs(target, 'parts', amounts['parts'] // 100 * qmult, qmult, 100 * rate,
                                issue_excess=25 if native == 'device-batch-a' else 15)
                for tag, cents in amounts.items():
                    if tag != 'parts':
                        voucher(target, tag, cents * mult, 'production')
                lifecycle(target, *(x * qmult for x in counts))
                weights[target] = mult; budgets[target] = budget * mult
            for native, tag, cents, classification in [
                ('period-registration', 'registration_testing', 12000, 'period'),
                ('period-logistics', 'warehouse_logistics', 6000, 'period'),
                ('service-warranty', 'actual_warranty', 8000, 'service'),
                ('service-normal', 'after_sales', 4000, 'service')]:
                target = oid(native, g); voucher(target, tag, cents * mult, classification); budgets[target] = cents * mult
            shared_amounts = dict(depreciation=12000, utilities=8000)
        for tag, cents in shared_amounts.items():
            shared_expected[tag] += cents * mult
        cash = 12345 * mult
        identity = f'CASH-{industry}-{g + 1}'
        finance.append(dict(**scope, id=identity, kind='cash_movement', status='data', amount=money(cash),
                            economic_key=identity, evidence='synthetic payment memo; explicitly excluded from costs',
                            tag='cash_only', target_object_id='', origin_period=''))
        excluded_cash += cash

    for tag, monthly in shared_expected.items():
        kind = 'asset_basis' if tag == 'depreciation' else 'prepay_basis' if tag == 'licence_amortization' else 'expense'
        shared_tags[tag] = dict(kind=kind, element=tag, **({'months': 12} if kind != 'expense' else {}))
        base = monthly * 12 if kind != 'expense' else monthly
        count = 1 if kind != 'expense' else min(repeats, base)
        for piece, amount in enumerate(fragments(base, count, rng), 1):
            identity = f'SHARED-{tag}-{piece:03}'
            shared.append(dict(**scope, id=identity, kind=kind, status='data', amount=money(amount),
                               economic_key=identity, evidence='synthetic approved allocation/schedule ' + identity,
                               tag=tag, target_object_id='', origin_period='2026-01' if kind != 'expense' else ''))
        allocations[tag] = partition(monthly, period_weights if tag == 'head_office' else weights)
        for target, cents in allocations[tag].items():
            direct[target][tag] += cents

    if industry == 'medical_devices':
        # Source-declared approved unit registry, not a serial number or a
        # claim that physical enterprise identities have been independently
        # audited. Every disclosed monetary rework source and each scrap/
        # return unit is covered; no identity belongs to two dispositions.
        def disposition(target, action, index, ref):
            token = f'{target}-{action}-{index:04}'
            dispositions.append(dict(**scope, id='DSP-' + token, object_id=target,
                                     item_id='APPROVED-UNIT-' + token, action=action, ref=ref))
        for index, row in enumerate((x for x in finance if x['tag'] == 'rework'), 1):
            disposition(row['target_object_id'], 'rework', index, row['id'])
        for row in events:
            if row['op'] in {'scrap', 'return'}:
                for index in range(1, int(row['quantity']) + 1):
                    disposition(row['object_id'], row['op'], index, row['id'])

    categories = defaultdict(int)
    expected_objects = {}
    life = {}
    period_total = service_total = 0
    for identity, meta in objects.items():
        cost = sum(direct[identity].values())
        expected_objects[identity] = dict(total_cost=money(cost), cost_structure={k: money(v) for k, v in direct[identity].items()},
                                          classification=meta['classification'], budget=money(budgets[identity]), variance=money(cost - budgets[identity]))
        for tag, cents in direct[identity].items(): categories[tag] += cents
        if meta['classification'] == 'period': period_total += cost
        if meta['classification'] == 'service': service_total += cost
        if identity in quantities:
            q = quantities[identity]
            qwip = q['start'] - q['complete'] - q['scrap']
            qfg = q['complete'] - q['sale'] + q['returned']
            buckets = dict(wip=qwip, finished_inventory=qfg, cost_of_sales=q['sale'] - q['returned'], scrap_loss=q['scrap'])
            values = partition(cost, buckets)
            assert sum(buckets.values()) == q['start']
            life[identity] = dict(started_quantity=str(q['start']), completed_quantity=str(q['complete']),
                                  sales_quantity=str(q['sale']), returned_quantity=str(q['returned']),
                                  wip_quantity=str(qwip), finished_quantity=str(qfg), scrap_quantity=str(q['scrap']),
                                  **{k: money(v) for k, v in values.items()},
                                  qualified_unit_cost=str(D(values['finished_inventory'] + values['cost_of_sales']) / 100 / q['complete']))
    total = sum(categories.values())
    asset = sum(round(D(v['wip']) * 100) + round(D(v['finished_inventory']) * 100) for v in life.values())
    pnl = total - asset
    expected = dict(oracle='independent integer cents + Fraction largest remainder; no costing-engine import',
                    seed=SEED, variant='large' if large else 'small', industry=industry, scope=scope,
                    source_rows=dict(finance=len(finance), shared=len(shared), events=len(events), settlements=len(settlements), stock=len(stock)),
                    total_cost=money(total), cost_groups={k: money(v) for k, v in categories.items()}, objects=expected_objects,
                    lifecycle=life, asset_total=money(asset), pnl_cost=money(pnl), period_expense=money(period_total),
                    service_cost=money(service_total), budget_total=money(sum(budgets.values())),
                    budget_variance=money(total - sum(budgets.values())), excluded_cash=money(excluded_cash),
                    can_claim_entire_industry=False)
    if settlement_want:
        expected['settlement'] = dict(rows={k: {a: money(b) for a, b in v.items()} for k, v in settlement_want.items()},
                                      totals={key: money(sum(v[key] for v in settlement_want.values())) for key in next(iter(settlement_want.values()))})
    if stock:
        expected['inventory'] = {k: money(v) for k, v in stock_totals.items()}
        expected['inventory']['reconciliation_difference'] = '0.00'
    if menu_dimensions:
        expected['menu_dimensions'] = menu_dimensions
        expected['head_office_allocation'] = {
            target: dict(approved_portions=weight, amount=money(allocations['head_office'][target]))
            for target, weight in period_weights.items()
        }
    if dispositions:
        expected['source_rows']['dispositions'] = len(dispositions)
        expected['dispositions'] = dict(total_count=len(dispositions),
                                        counts={a: sum(r['action'] == a for r in dispositions) for a in ('rework', 'scrap', 'return')},
                                        identity_basis='synthetic approved unit identity registry v1',
                                        evidence_scope='source_declared_disposition_identity;not_serial_trace')
    if raw_materials:
        expected['source_rows']['raw_materials'] = len(raw_materials)
        expected['raw_materials'] = dict(streams=raw_material_expected,
            **{field: format(sum((D(s[field]) for s in raw_material_expected.values()), D(0)), '.2f')
               for field in ('available_value', 'net_production_consumption', 'closing_value')})
    files = dict(finance=csv_text(finance, FIN_FIELDS), shared=csv_text(shared, FIN_FIELDS), events=csv_text(events, EVENT_FIELDS))
    if settlements: files['settlements'] = csv_text(settlements, SETTLEMENT_FIELDS)
    if stock: files['stock'] = csv_text(stock, STOCK_FIELDS)
    if dispositions: files['dispositions'] = csv_text(dispositions, DISPOSITION_FIELDS)
    if raw_materials: files['raw_materials'] = csv_text(raw_materials, MATERIAL_FIELDS)
    return dict(industry=industry, scope=scope, files=files,
                descriptor=dict(objects=list(objects.values()), finance_tags=finance_tags, shared_tags=shared_tags,
                                allocation_weights=weights, period_weights=period_weights, budgets=budgets,
                                settlement_objects=list(settlement_want), stock_streams=stock_streams,
                                raw_material_streams=raw_material_streams), expected=expected)


def write(industry, destination, large=False):
    from profile_contract import make_schema, coverage_dictionary, instructions
    bundle = build(industry, large)
    destination.mkdir(parents=True, exist_ok=True)
    for name, text in bundle['files'].items():
        (destination / (name + '.csv')).write_text(text, encoding='utf-8', newline='')
    (destination / 'rules.json').write_text(json.dumps(make_schema(bundle), ensure_ascii=False, indent=2), encoding='utf-8')
    (destination / 'expected.json').write_text(json.dumps(bundle['expected'], ensure_ascii=False, indent=2), encoding='utf-8')
    if not large:
        (destination / 'field_dictionary.md').write_text(coverage_dictionary(industry), encoding='utf-8')
        (destination / 'README.md').write_text(instructions(industry), encoding='utf-8')
    return bundle['expected']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--industry', choices=INDUSTRIES, action='append')
    parser.add_argument('--output', type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument('--large', action='store_true')
    args = parser.parse_args()
    for industry in args.industry or INDUSTRIES:
        target = args.output / industry / ('large' if args.large else '')
        expected = write(industry, target, args.large)
        print(json.dumps(dict(industry=industry, variant=expected['variant'], rows=expected['source_rows'], total=expected['total_cost']), ensure_ascii=False))


if __name__ == '__main__':
    main()
