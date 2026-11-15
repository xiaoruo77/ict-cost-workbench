"""Server-only binding for a guarded, read-only monthly comparison preview.

No provider is configured here. A real provider must derive these claims
independently of caller/uploads, and read both bindings atomically from the
same authoritative registry view. Book registration is not ledger attestation.
"""
from collections.abc import Mapping
from dataclasses import dataclass
import re
from typing import Any, Protocol

from cost_core.object_rollups import safe_text
from costing_monthly import MonthlyComparisonError, MonthlyRevision, compare_canonical_months
from costing_outputs import validate_snapshot_integrity

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True, slots=True)
class MonthlyRevisionPin:
    analysis_id: str
    revision: str


@dataclass(frozen=True, slots=True)
class MonthlyBindingClaim:
    owner: str
    pin: MonthlyRevisionPin
    book_uid: str
    declared_book: str
    policy_sha256: str


@dataclass(frozen=True, slots=True)
class MonthlyBindingPair:
    current: MonthlyBindingClaim
    previous: MonthlyBindingClaim


class MonthlyBindingProvider(Protocol):
    def load_pair(
        self, *, expected_owner: str, current: MonthlyRevisionPin, previous: MonthlyRevisionPin
    ) -> MonthlyBindingPair:
        """Return exactly these owner-scoped bindings from one read-only registry view."""
        ...


class MonthlyPreviewUnavailable(ValueError):
    """HTTP adapter maps to the same 404 as absent or inaccessible snapshots."""


class MonthlyPreviewIncomparable(ValueError):
    """HTTP adapter maps to one generic 422 after authorization of both sides."""


def _digest(value, length):
    return type(value) is str and (_HEX32 if length == 32 else _HEX64).fullmatch(value) is not None


def _valid_pin(pin):
    return (
        type(pin) is MonthlyRevisionPin
        and _digest(pin.analysis_id, 32)
        and _digest(pin.revision, 64)
    )


def _owned_exact(snapshot, pin, owner):
    if not isinstance(snapshot, Mapping) or (
        snapshot.get("analysis_id"), snapshot.get("revision"),
        snapshot["context"].get("owner_external_id"),
    ) != (pin.analysis_id, pin.revision, owner):
        raise MonthlyPreviewUnavailable
    validate_snapshot_integrity(snapshot, pin.analysis_id)


def _claim(claim, pin, owner, snapshot):
    if (
        type(claim) is not MonthlyBindingClaim
        or type(claim.owner) is not str
        or claim.owner != owner
        or not _valid_pin(claim.pin)
        or claim.pin != pin
        or not safe_text(claim.book_uid, 128)
        or not safe_text(claim.declared_book, 128)
        or not _digest(claim.policy_sha256, 64)
        or claim.declared_book != snapshot["result"]["recognition"]["context"]["book"]
    ):
        raise MonthlyPreviewUnavailable


def guarded_monthly_preview(
    *,
    current_snapshot: Mapping[str, Any],
    previous_snapshot: Mapping[str, Any],
    current_pin: MonthlyRevisionPin,
    previous_pin: MonthlyRevisionPin,
    expected_owner: str,
    authority: MonthlyBindingProvider | None,
) -> dict[str, Any]:
    """Compare loaded snapshots; caller must authenticate and load in tenant scope."""
    if authority is None or not safe_text(expected_owner, 256) or not (
        _valid_pin(current_pin) and _valid_pin(previous_pin)
    ):
        raise MonthlyPreviewUnavailable

    try:
        _owned_exact(current_snapshot, current_pin, expected_owner)
        _owned_exact(previous_snapshot, previous_pin, expected_owner)
        bindings = authority.load_pair(
            expected_owner=expected_owner, current=current_pin, previous=previous_pin
        )
        if type(bindings) is not MonthlyBindingPair:
            raise MonthlyPreviewUnavailable
        _claim(bindings.current, current_pin, expected_owner, current_snapshot)
        _claim(bindings.previous, previous_pin, expected_owner, previous_snapshot)
    except Exception:
        # A missing, misconfigured, or unavailable authority cannot reveal
        # either snapshot or an internal error/registry response.
        raise MonthlyPreviewUnavailable from None

    current, previous = bindings.current, bindings.previous
    if (current.book_uid, current.declared_book, current.policy_sha256) != (
        previous.book_uid, previous.declared_book, previous.policy_sha256
    ):
        raise MonthlyPreviewIncomparable

    try:
        result = compare_canonical_months(
            previous_snapshot,
            current_snapshot,
            expected_workspace_owner=expected_owner,
            expected_declared_book=current.declared_book,
            prior_pin=MonthlyRevision(previous_pin.analysis_id, previous_pin.revision, previous.policy_sha256),
            current_pin=MonthlyRevision(current_pin.analysis_id, current_pin.revision, current.policy_sha256),
        )
    except MonthlyComparisonError as exc:
        if exc.code in {"requires_full_canonical_actuals", "incomparable_ledger_scope", "months_not_adjacent",
                        "incomparable_accounting_policy"}:
            raise MonthlyPreviewIncomparable from None
        raise MonthlyPreviewUnavailable from None
    except (KeyError, TypeError, ValueError, ArithmeticError, AttributeError, IndexError):
        raise MonthlyPreviewUnavailable from None

    # Public preview carries no authority registry values and never attests
    # accounting correctness or completeness, even with matching claims.
    result.pop("policy_sha256", None)
    return result
