"""Compatibility view over core arithmetic and pharmaceutical display labels."""
from cost_core.numbers import as_decimal as _strict_decimal
from cost_core.numbers import quantize as _quantize,format_number as _format_number,compare_values
from industry_plugins.pharma.policy import DIFF_BASE,DIFF_FORMULA,DIFF_NOTE,LEGACY_DIRECTIONS

def as_decimal(value):
    # Older report readers also pass percentages. The normalized core/adapter
    # parsers remain strict and never interpret percent strings as currency.
    return _strict_decimal(value.strip().rstrip('%') if isinstance(value,str) else value)
def quantize(value,places=2):return _quantize(as_decimal(value),places)
def format_number(value,places=2,signed=False):return _format_number(as_decimal(value),places,signed)
def compare_cost(self_value,peer_value):
    result=compare_values(as_decimal(self_value),as_decimal(peer_value))
    number=lambda v:float(quantize(v)) if v is not None else None
    return {'self_value':number(result['self_value']),'peer_value':number(result['peer_value']),
            'difference':number(result['difference']),'rate':number(result['rate']),
            'direction':LEGACY_DIRECTIONS[result['direction']]}
