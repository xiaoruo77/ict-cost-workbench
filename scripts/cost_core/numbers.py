"""Pure Decimal arithmetic; presentation is the only rounding boundary."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP, localcontext
import re
from typing import Any, Optional

_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?\Z")
_GROUPED = re.compile(r"[+-]?\d{1,3}(?:,\d{3})+(?:\.\d*)?(?:[eE][+-]?\d+)?\Z")


def as_decimal(value: Any) -> Optional[Decimal]:
    if value is None or isinstance(value, bool):
        return None
    text = str(value).strip()
    if not (_NUMBER.fullmatch(text) or _GROUPED.fullmatch(text)):
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def quantize(value: Any, places: int = 2) -> Optional[Decimal]:
    if not isinstance(places, int) or isinstance(places, bool) or not 0 <= places <= 20:
        raise ValueError("places must be an integer from 0 to 20")
    number = as_decimal(value)
    if number is None:
        return None
    with localcontext() as context:
        context.prec = max(34, len(number.as_tuple().digits) + abs(number.adjusted()) + places + 4)
        result = number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return abs(result) if result == 0 else result


def format_number(value: Any, places: int = 2, signed: bool = False) -> str:
    number = quantize(value, places)
    return "—" if number is None else format(number, f"{'+' if signed else ''}.{places}f")


def pct_change(current: Any, baseline: Any) -> Optional[Decimal]:
    current, baseline = as_decimal(current), as_decimal(baseline)
    if current is None or baseline is None or baseline == 0:
        return None
    return (current - baseline) / baseline * Decimal(100)


def compare_values(self_value: Any, peer_value: Any) -> dict:
    own, peer = as_decimal(self_value), as_decimal(peer_value)
    available = own is not None and peer is not None
    difference = peer - own if available else None
    return {
        "self_value": own, "peer_value": peer,
        "difference": difference,
        "rate": pct_change(peer, own),
        "direction": ("unavailable" if not available else "peer_higher" if difference > 0
                      else "self_higher" if difference < 0 else "equal"),
    }
