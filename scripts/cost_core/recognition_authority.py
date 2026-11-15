"""Optional trusted comparison; HTTP upload does not create the authority."""
from dataclasses import dataclass
from collections.abc import Mapping
from .source_book_manifest import Scope, compare_manifest, Result

@dataclass(frozen=True)
class VerifiedSourceBookComparison:
    """Server/internal caller constructs only after checking independent authority.

    A manifest's self hash is NOT a signature. Authenticated workspace and the
    scoped upstream snapshot version are part of externally verified context.
    """
    manifest: Mapping
    expected: Scope
    independently_verified_authority: str

def compare_source_population(proof, raw_sources, tags, row_metadata, actual_context):
    if proof is None:
        return dict(status="not_provided", population="declared_monetary_source_rows_only")
    if not isinstance(proof,VerifiedSourceBookComparison):
        raise ValueError("recognition: trusted source comparison must come from the server")
    expected=proof.expected
    if (expected.book,expected.period,expected.currency)!=(actual_context["book"],actual_context["period"],actual_context["currency"]):
        checked=Result("mismatch",("recognition_scope_mismatch",))
    elif expected.scale!=2:
        checked=Result("mismatch",("recognizer_v1_requires_two_decimal_currency",))
    else:
        rows=[]
        for x in raw_sources:
            if x["amount"] is None:
                # Preserve unknown/nonmonetary input as such. Never convert it
                # into an authoritative currency zero.
                continue
            sid=x["id"]
            metadata={k:x[k] for k in ("status","economic_key","origin_period","bridge_group") if k in x}
            if tags.get(sid):metadata["tag"]=tags[sid]
            target=row_metadata.get(sid,{}).get("target_object_id")
            if target:metadata["target_object_id"]=target
            rows.append({**{k:x[k] for k in ("id","book","period","currency","kind","amount")},"tags":metadata})
        checked=compare_manifest(manifest=proof.manifest,rows=rows,expected=expected,
                                 trusted_authority_id=proof.independently_verified_authority)
    return dict(status=checked.status,reasons=checked.reasons,missing_ids=checked.missing_ids,extra_ids=checked.extra_ids,
                changed_ids=checked.changed_ids, population="declared_monetary_source_rows_only",
                upstream_scope=expected.scope,upstream_version=expected.version,
                nonmonetary_ids=tuple(x["id"] for x in raw_sources if x["amount"] is None))
