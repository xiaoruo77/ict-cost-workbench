"""Evaluate configured object-level coverage against the declared cost ledger."""
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import asdict
from decimal import Decimal, InvalidOperation, localcontext
import csv
import io
import unicodedata
from urllib.parse import parse_qsl

from .models import SourceRef
from .recognition_eval import Status


def need(condition, message):
    if not condition:
        raise ValueError('required_costs: ' + message)


def valid_id(value):
    return (isinstance(value, str) and 0 < len(value) <= 256 and value == value.strip()
            and not any(unicodedata.category(char).startswith('C') for char in value))


def folded(value):
    return unicodedata.normalize('NFKC', unicodedata.normalize('NFKC', value).casefold())


def compile_required_costs(files, schema, source_uris=None, *, recognized_entries=()):
    """Compile declared object requirements and retain admissible zero evidence.

    ``recognized_entries`` contains the adapter's typed postings. The engine
    removes zero components when it nets postings, so a typed zero may supply
    evidence only when its original financial rows explicitly contain zero.
    """
    business = schema.get('business_views') or {}
    if 'required_costs' not in business:
        return None
    requirements = business['required_costs']
    classifications = business.get('classifications', {})
    need(isinstance(requirements, dict) and 1 <= len(requirements) <= 1000,
         'expected a nonempty object-to-elements mapping')
    need(all(valid_id(object_id) and object_id in classifications for object_id in requirements),
         'required cost object must be declared in classifications')
    need(len({folded(object_id) for object_id in requirements}) == len(requirements),
         'aliased required cost object IDs')
    for object_id, elements in requirements.items():
        need(isinstance(elements, list) and 1 <= len(elements) <= 128 and all(valid_id(item) for item in elements),
             f'{object_id}: expected nonempty cost element ID list')
        need(len({folded(item) for item in elements}) == len(elements), f'{object_id}: duplicate or aliased cost elements')
    need(sum(map(len, requirements.values())) <= 10000, 'too many declared requirements')

    zero_refs = _compile_zero_refs(files, schema, source_uris, requirements, recognized_entries)
    policy_ref = asdict(SourceRef(dataset_id=schema.get('dataset_id', 'csv-unregistered'),
                        source_uri='upload://schema', field='business_views', selector='business_views',
                        kind='configuration'))
    return {'requirements': {key: tuple(value) for key, value in requirements.items()},
            'zero_refs': zero_refs, 'policy_ref': policy_ref}


def _compile_zero_refs(files, schema, source_uris, requirements, entries):
    finance_sources = {}
    for branch in schema.get('raw_composite', {}).get('inputs', {}).values():
        if 'raw_recognition' not in branch:
            continue
        file = branch['file']
        source_uri = (source_uris or {}).get(file, f'upload://sources/{file}.csv')
        columns = branch.get('columns', {})
        finance_sources[source_uri] = (file, {key: columns.get(key, key) for key in ('id','status','amount','evidence')})
    loaded = {}
    zero = defaultdict(list)
    for entry in entries:
        if entry.amount != 0 or entry.element_id not in requirements.get(entry.object_id, ()):
            continue
        references = [ref for ref in entry.source_refs if ref.kind == 'raw']
        if not references or not all(_is_original_zero(ref, files, finance_sources, loaded) for ref in references):
            continue
        zero[(entry.object_id, entry.element_id)].extend(asdict(ref) for ref in references)
    return dict(zero)


def _is_original_zero(ref, files, finance_sources, loaded):
    if ref.source_uri not in finance_sources:
        return False
    file, columns = finance_sources[ref.source_uri]
    selector = dict(parse_qsl(ref.selector, keep_blank_values=True))
    if ref.field != columns['amount'] or not selector.get('row', '').isdigit() or columns['id'] not in selector:
        return False
    if file not in loaded:
        loaded[file] = list(csv.DictReader(io.StringIO(files[file].lstrip('\ufeff')), strict=True))
    index = int(selector['row']) - 2
    if not 0 <= index < len(loaded[file]):
        return False
    row = loaded[file][index]
    if (row.get(columns['id'], '').strip() != selector[columns['id']]
            or row.get(columns['status'], '').strip() not in {Status.DATA.value, Status.ZERO.value}
            or not row.get(columns['evidence'], '').strip()):
        return False
    try:
        value = Decimal(row.get(columns['amount'], '').strip())
    except InvalidOperation:
        return False
    return value.is_finite() and value == 0


def _unique_references(refs):
    return list({tuple(sorted(ref.items())): ref for ref in refs}.values())


def check_required_costs(plan, objects):
    if plan is None:
        return None
    by_id = objects if isinstance(objects, Mapping) else {obj['object_id']: obj for obj in objects}
    policy = plan['policy_ref']
    rows = []
    for object_id, elements in plan['requirements'].items():
        if object_id not in by_id:
            # Object-scoped analysis has already selected which rows to expose.
            continue
        components = defaultdict(list)
        for part in by_id[object_id]['cost_components']:
            if Decimal(part['amount']) != 0:
                components[part['element_id']].append(part)
        for element in elements:
            postings = components[element]
            zero_refs = plan['zero_refs'].get((object_id, element), ())
            if postings:
                status = 'observed'
                with localcontext() as context:
                    context.prec = 60
                    amount = format(sum((Decimal(part['amount']) for part in postings), Decimal(0)), 'f')
                refs = [ref for part in postings for ref in part['source_refs']]
            elif zero_refs:
                status, amount, refs = 'explicit_zero', '0.00', []
            else:
                status, amount, refs = 'missing', None, []
            rows.append({'object_id': object_id, 'element_id': element, 'status': status, 'amount': amount,
                         'source_refs': _unique_references([policy, *refs, *zero_refs])})
    missing = [{'object_id': row['object_id'], 'element_id': row['element_id']}
               for row in rows if row['status'] == 'missing']
    return {'version': 1, 'status': 'incomplete' if missing else 'satisfied_for_declared_requirements',
            'summary': {'required_count': len(rows), 'observed_count': sum(row['status'] == 'observed' for row in rows),
                        'explicit_zero_count': sum(row['status'] == 'explicit_zero' for row in rows),
                        'missing_count': len(missing)},
            'rows': rows, 'missing': missing, 'source_refs': [policy]}
