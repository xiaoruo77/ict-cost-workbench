"""Configured lifecycle and settlement projections of the existing cost ledger.

This module never posts an additional cost. Homogeneous equivalent-unit pools
are an explicit policy boundary, not an automatic accounting judgement.
"""
import csv
import io
import re
import unicodedata
from decimal import Decimal as D, localcontext
from urllib.parse import urlencode


def need(ok, message):
    if not ok:
        raise ValueError('business_views: ' + message)


def number(value, location, money=False):
    pattern = r'[0-9]{1,18}(?:\.[0-9]{1,2})?' if money else r'[0-9]{1,18}(?:\.[0-9]{1,6})?'
    need(isinstance(value, str) and re.fullmatch(pattern, value) is not None,
         location + ': required nonnegative decimal' + (' money' if money else ' quantity'))
    return D(value)


def key(value):
    return unicodedata.normalize('NFKC', str(value)).casefold().strip()


def compile_business_views(files, schema, source_uris=None, *, recognized_entries=()):
    cfg = schema.get('business_views')
    if cfg is None:
        return None
    need(isinstance(cfg, dict) and set(cfg) <= {'version', 'policy_source', 'classifications', 'lifecycle', 'settlement', 'dispositions', 'notices', 'required_costs', 'financial_ledgers'}, 'unknown configuration fields')
    need(type(cfg.get('version')) is int and cfg['version'] == 1 and bool(cfg.get('policy_source')), 'version and approved policy source required')
    kinds = cfg.get('classifications')
    need(isinstance(kinds, dict) and kinds and all(v in {'production', 'period', 'service'} for v in kinds.values()), 'explicit object classifications required')
    scope = schema.get('raw_composite', {}).get('scope')
    need(isinstance(scope, dict), 'common ledger scope required')
    plan = {'version': 1, 'policy_source': cfg['policy_source'], 'classifications': dict(kinds), 'scope': dict(scope), 'notices': cfg.get('notices', []),
            'policy_refs': [{'dataset_id': schema.get('dataset_id', 'csv-unregistered'), 'source_uri': 'upload://schema', 'field': 'business_views', 'selector': 'business_views', 'kind': 'configuration', 'formula': ''}]}
    seen = set()
    for name, fields in [('lifecycle', {'id', 'object_id', 'op', 'quantity', 'unit', 'ref'}), ('settlement', {'id', 'object_id', 'op', 'amount', 'ref'})]:
        if name not in cfg:
            continue
        opt = cfg[name]
        extra = {'units'} if name == 'lifecycle' else {'commission_element', 'payment_fee_element', 'merchant_discount_treatment', 'platform_discount_treatment'}
        required = {'file', 'columns'} | extra
        optional = {'expected_objects'} if name == 'settlement' else set()
        need(isinstance(opt, dict) and required <= set(opt) <= required | optional, name + ': invalid options')
        if name == 'settlement':
            fees = [opt['commission_element'], opt['payment_fee_element']]
            need(all(isinstance(fee, str) and fee.strip() for fee in fees)
                 and key(fees[0]) != key(fees[1]), 'settlement: commission and payment fee require distinct cost elements')
        if 'expected_objects' in opt:
            expected = opt['expected_objects']
            need(isinstance(expected, list) and expected and all(isinstance(oid, str) and oid in kinds for oid in expected)
                 and len(set(expected)) == len(expected), 'settlement: invalid expected objects')
        need(opt['file'] in files, name + ': missing source file ' + str(opt['file']))
        cols = opt['columns']
        need(isinstance(cols, dict) and set(cols) == fields and len(set(cols.values())) == len(cols), name + ': invalid column mapping')
        reader = csv.DictReader(io.StringIO(files[opt['file']].lstrip('\ufeff')))
        headers = reader.fieldnames or []
        need(len(headers) == len(set(headers)) and set(headers) == set(cols.values()) | {'book', 'period', 'currency'}, name + ': unknown/missing/duplicate headers')
        records = []
        for lineno, raw in enumerate(reader, 2):
            where = f'{opt["file"]}:row {lineno}'
            need(None not in raw and all(v is not None for v in raw.values()), where + ': ragged row')
            need(all(raw[k] == v for k, v in scope.items()), where + ': book/period/currency mismatch')
            row = {k: raw[v].strip() for k, v in cols.items()}
            need(row['id'] and key(row['id']) not in seen, where + ': duplicate or aliased source id')
            seen.add(key(row['id']))
            need(row['object_id'] in kinds, where + ': unknown object')
            row['source_refs'] = [{'dataset_id': schema.get('dataset_id', 'csv-unregistered'), 'source_uri': (source_uris or {}).get(opt['file'], 'upload://sources/' + opt['file'] + '.csv'), 'field': cols['quantity' if name == 'lifecycle' else 'amount'], 'selector': urlencode({'row': lineno, 'id': row['id']}), 'kind': 'raw', 'formula': ''}]
            row['value'] = str(number(row['quantity' if name == 'lifecycle' else 'amount'], where, name == 'settlement'))
            records.append(row)
        need(records and len(records) <= 20000, name + ': empty source or record limit')
        plan[name] = {**opt, 'records': records}
    need(isinstance(plan['notices'], list) and all(isinstance(n, dict) and set(n) == {'code', 'message'} and all(isinstance(v, str) and v for v in n.values()) for n in plan['notices']), 'invalid notices')
    if 'dispositions' in cfg:
        from .disposition_controls import compile_dispositions
        plan['dispositions'] = compile_dispositions(files,schema,source_uris)
    if 'required_costs' in cfg:
        from .required_costs import compile_required_costs
        plan['required_costs'] = compile_required_costs(files, schema, source_uris, recognized_entries=recognized_entries)
    if 'financial_ledgers' in cfg:
        from .financial_ledgers import compile_financial_ledgers
        plan['financial_ledgers'] = compile_financial_ledgers(files, schema, source_uris, recognized_entries=recognized_entries)
    if 'financial_ledgers' in cfg or schema.get('raw_composite', {}).get('operational_inputs'):
        plan['presentation_version'] = 4
    return plan


def evaluate_business_views(plan, result, apportion):
    with localcontext() as ctx:
        ctx.prec = 60
        return _evaluate(plan, result, apportion)


def _evaluate(plan, result, apportion):
    kinds = plan['classifications']
    objects = {r['object_id']: r for r in result['objects']}
    need(all(oid in objects for oid in kinds), 'classification references unknown cost object')
    need(all(r['object_id'] in kinds or D(r['total_cost']) == 0 for r in result['objects']), 'unclassified nonzero ledger object')
    out = {'version': 1, 'presentation_version': plan.get('presentation_version', 2), 'policy_source': plan['policy_source'], 'scope': plan['scope'], 'source_refs': plan.get('policy_refs', []), 'basis': 'homogeneous_equivalent_units; incurred_cost_is_not_pnl', 'notices': plan['notices'], 'lifecycle': [], 'period_expense': '0.00', 'service_cost': '0.00'}
    for oid, kind in kinds.items():
        if kind in {'period', 'service'}:
            field = 'period_expense' if kind == 'period' else 'service_cost'
            out[field] = str(D(out[field]) + D(objects[oid]['total_cost']))
    production = {oid for oid, v in kinds.items() if v == 'production'}
    events = plan.get('lifecycle', {}).get('records', [])
    units = plan.get('lifecycle', {}).get('units', {})
    need(set(units) == production, 'production objects require explicit units and event source')
    balances = {oid: dict(start=D(0), wip=D(0), complete=D(0), complete_evidenced=False, fg=D(0), sale=D(0), returned=D(0), scrap=D(0), source_refs=[]) for oid in sorted(production)}
    sales = {}
    for row in events:
        oid, op, q = row['object_id'], row['op'], D(row['value'])
        need(oid in production and row['unit'] == units[oid], f'{row["id"]}: object/unit mismatch')
        b = balances[oid]
        need(op in {'start', 'complete', 'sale', 'return', 'scrap'}, row['id'] + ': unsupported lifecycle event')
        need(not row['ref'] or op == 'return', row['id'] + ': unexpected reference')
        if op == 'start':
            b['start'] += q; b['wip'] += q
        elif op in {'complete', 'scrap'}:
            need(q <= b['wip'], row['id'] + ': exceeds available work in progress')
            b['wip'] -= q; b[op] += q
            if op == 'complete':
                b['fg'] += q; b['complete_evidenced'] = True
        elif op == 'sale':
            need(q <= b['fg'], row['id'] + ': sale exceeds finished stock')
            b['fg'] -= q; b['sale'] += q
            sales[row['id']] = [oid, q, D(0)]
        else:
            original = sales.get(row['ref'])
            need(original is not None and original[0] == oid and original[2] + q <= original[1], row['id'] + ': missing original sale or excessive return')
            original[2] += q; b['fg'] += q; b['returned'] += q
        b['source_refs'].extend(row['source_refs'])
    for oid, b in balances.items():
        cost = D(objects[oid]['total_cost'])
        need(cost >= 0 and (b['start'] > 0 or cost == 0), oid + ': cannot value positive cost without started quantity')
        weights = {'wip': b['wip'], 'finished_inventory': b['fg'], 'cost_of_sales': b['sale'] - b['returned'], 'scrap_loss': b['scrap']}
        need(sum(weights.values(), D(0)) == b['start'], oid + ': quantity bridge failed')
        # Reuse the engine's nonnegative largest-remainder monetary allocation.
        amounts = apportion(cost, weights, D('.01')) if b['start'] > 0 else {k: D(0) for k in weights}
        if isinstance(amounts, tuple): amounts = amounts[0]
        need(sum(amounts.values(), D(0)) == cost, oid + ': lifecycle minor-unit conservation failed')
        complete_cost = amounts['finished_inventory'] + amounts['cost_of_sales']
        out['lifecycle'].append({'object_id': oid, 'unit': units[oid], 'incurred_cost': str(cost), 'started_quantity': str(b['start']), 'completed_quantity': str(b['complete']) if b['complete_evidenced'] else None, 'sales_quantity': str(b['sale']), 'returned_quantity': str(b['returned']), 'wip_quantity': str(b['wip']), 'finished_quantity': str(b['fg']), 'scrap_quantity': str(b['scrap']), **{k: str(v) for k, v in amounts.items()}, 'qualified_unit_cost': str(complete_cost / b['complete']) if b['complete'] > 0 else None, 'denominator_status': 'provided' if b['complete'] > 0 else 'explicit_zero' if b['complete_evidenced'] else 'missing', 'source_refs': b['source_refs'] + objects[oid]['source_refs'], 'cost_components': objects[oid]['cost_components']})
    out['asset_total'] = str(sum((D(r['wip']) + D(r['finished_inventory']) for r in out['lifecycle']), D(0)))
    out['pnl_cost'] = str(D(out['period_expense']) + D(out['service_cost']) + sum((D(r['cost_of_sales']) + D(r['scrap_loss']) for r in out['lifecycle']), D(0)))
    out['incurred_total'] = result['totals']['total_cost']
    need(D(out['asset_total']) + D(out['pnl_cost']) == D(out['incurred_total']), 'asset plus pnl does not tie to incurred ledger')
    if 'settlement' in plan:
        out['settlement'] = _settlement(plan['settlement'], objects)
    if plan.get('dispositions'):
        out['dispositions'] = plan['dispositions']
    if plan.get('required_costs') is not None:
        from .required_costs import check_required_costs
        out['required_costs'] = check_required_costs(plan['required_costs'], result['objects'])
    if result.get('scope', {}).get('type') != 'object':
        inventory_assets = []
        for name, source in (result.get('recognition') or {}).get('inputs', {}).items():
            inventory = source.get('inventory_valuation')
            if inventory is not None:
                inventory_assets.append({
                    'input_name': name, 'closing_value': inventory['outputs']['closing_value'],
                })
        if inventory_assets:
            out['separate_inventory_assets'] = inventory_assets
    if plan.get('financial_ledgers') is not None:
        from .financial_ledgers import evaluate_financial_ledgers
        out['financial_ledgers'] = evaluate_financial_ledgers(plan['financial_ledgers'], result)
    return out


def _settlement(cfg, objects):
    need(cfg['merchant_discount_treatment'] == 'revenue_reduction' and cfg['platform_discount_treatment'] == 'platform_compensated', 'unsupported settlement recognition policy')
    groups = {}; originals = {}; refs = []; present = {}
    ops = {'gross', 'merchant_discount', 'platform_discount', 'refund', 'commission', 'payment_fee', 'receipt'}
    for row in cfg['records']:
        oid, op, amount = row['object_id'], row['op'], D(row['value'])
        need(op in ops, row['id'] + ': unsupported settlement operation')
        g = groups.setdefault(oid, {k: D(0) for k in ops})
        present.setdefault(oid,set()).add(op)
        if op == 'gross':
            need(not row['ref'], row['id'] + ': gross cannot reference another transaction')
            originals[row['id']] = {'object': oid, 'gross': amount, 'refund': D(0), 'merchant_discount': D(0)}
        else:
            original = originals.get(row['ref'])
            need(original is not None and original['object'] == oid, row['id'] + ': missing or mismatched original transaction')
            if op in {'refund', 'merchant_discount'}:
                original[op] += amount
                need(original['refund'] + original['merchant_discount'] <= original['gross'], row['id'] + ': excessive refund/discount')
        g[op] += amount; refs.extend(row['source_refs'])
    rows = []
    if 'expected_objects' in cfg:
        expected = set(cfg['expected_objects'])
        need(set(groups) == expected, 'settlement: missing objects ' + ','.join(sorted(expected - groups.keys()))
             + '; unexpected objects ' + ','.join(sorted(groups.keys() - expected)))
    for oid, g in groups.items():
        need(present[oid] == ops, oid + ': missing settlement operations ' + ','.join(sorted(ops - present[oid])) + '; provide explicit zero evidence instead of omitting')
        elements = objects[oid]['cost_structure']
        for op, configured in [('commission', 'commission_element'), ('payment_fee', 'payment_fee_element')]:
            need(D(elements.get(cfg[configured], '0')) == g[op], oid + ': settlement ' + op + ' must match exactly one ledger cost element')
        net = g['gross'] - g['merchant_discount'] - g['refund']
        due = net - g['commission'] - g['payment_fee']
        rows.append({'object_id': oid, **{k: str(v) for k, v in g.items()}, 'net_revenue': str(net), 'expected_receipt': str(due), 'receipt_difference': str(g['receipt'] - due)})
    return {'policy': 'merchant_discount_reduces_revenue;platform_discount_compensated;fees_expensed_once', 'rows': rows, 'totals': {k: str(sum((D(r[k]) for r in rows), D(0))) for k in [*sorted(ops), 'net_revenue', 'expected_receipt', 'receipt_difference']}, 'source_refs': refs}
