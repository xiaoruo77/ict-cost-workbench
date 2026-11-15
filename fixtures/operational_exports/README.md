# Validated operational-export conversion v1

This module is industry-neutral. It compiles approved structured CSV exports to physical object quantities and allocation drivers, and creates no money postings. It does not establish a live connection to any TMS, WMS, GPS vendor or verify that an enterprise approval is authentic.

## Contract and integration

- Optional envelope: raw_composite.operational_inputs. Version=1, rule_version and policy_source required.
- operational_files(spec) returns the auxiliary CSV keys; the bundle owner must include them in exact file-set/hash capture.
- compile_operational_bundle(spec, files, scope, dataset_id=...) returns objects, drivers keyed by allocation rule_id, generated_inputs={} and metadata. Metadata belongs under result.recognition.operational_inputs.
- An object binding contains object_id, metric_id, quantity_unit and optional explicit kind/name/currency/required. Consumers compare explicit financial declarations to source-backed quantities; they must not silently overwrite incompatible units, quantity or currency.
- Metadata.metrics is a dict metric_id -> object_id -> quantity/unit/status/source_refs. object_bindings additionally maps each final object to its metric/status. driver_bindings and relations retain their raw row references. unit_validation_status=source_verified only means uploaded rows agree with the declared contract, not vendor/approval authenticity.
- Raw selectors use row=<CSV record>&<actual id column>=<original id>, even when that column is event_id. Config binding refs point to raw_composite.operational_inputs.object_bindings[index].

## Independent fixture

Run generate_operational_fixture.py --out <isolated-directory>. It imports only the standard library. It writes six CSVs plus operational_inputs.json and expected-operational.json:

| Export | Raw rows | Source computation |
|---|---:|---|
| tms | 3 | 14000kg forward, 2000kg approved correction linked to its original; 8000kg second route |
| gps | 2 | end-start odometer =1000km for each leg; joined by leg_id |
| wms | 1 | 10 pallets, Oct20-Dec5 interval clipped to Nov1-Dec1 =30 days |
| orders | 100 | actual abstract completed-order event rows, quantity1 each |
| returns | 20 | actual abstract processing event rows, each links original ORDER and SKU, cumulative return <= original qty |
| activity | 5 | start/end work timestamps yield4/3/2/2/1h |

Independent quantities: route-self=(14-2)t*1000km=12000 tonne_km, route-outsourced=8t*1000km=8000 tonne_km; warehouse-A=10*30=300 pallet_day; delivery-city=100 completed_order; reverse-return=20 processed_return. Shared-driver weights are4:3:2:2:1 from time events. The original finance amounts are not used to derive these expectations.

## Units and derived factors

unit_registry explicitly declares dimensions, SI-base factor and optional aliases/quantity_kind. Common physical symbols and known composites cannot be redefined (kg/g/tonne/t,m/km,second/s/minute/min/hour/h/day,tonne_km,pallet,pallet_day). Example tonne.factor=1000,km.factor=1000,tonne_km.factor=1000000; second.factor=1,day.factor=86400. Business count units require a quantity_kind; equal arithmetic dimensions alone do not allow exchanging completed orders for returns.

A metric has primary event source, output unit,1..4 factors, optional match and joins. Factors:
- field: actual numeric field plus raw unit_field and approved unit.
- difference: non-negative end_field-start_field, actual unit_field; no silent odometer reset or guessed missing distance.
- duration: timezone-aware ISO timestamps, optional explicit open_end=scope_end, clip_to_period defaults true. It uses actual overlapping seconds and approved second/time anchor. period_timezone must explicitly declare the fiscal boundary (UTC, approved fixed offset or an available IANA zone); it never guesses the enterprise timezone. No naive timestamps or guessed missing ends.
- power may be-2,-1,1,2; zero denominators fail.

The output dimension must equal the factor expression. Unit aliases cannot form shadows/cycles or change standard dimension/factor/kind. Unknown/nonfinite/negative conversion factors fail. Approved conversions are deterministic; no generic assumption converts all counts, packages or SKUs.

## Event, scope and reversal rules

All source rows have exact book/reportingperiod/currency. Event sources map actual id/object_id/action; lookup sources need id and scope. Event IDs and headers are checked for duplicate/Unicode/case aliases. Every source row must be used by some metric or relation; ragged/unmapped data fails.

action=reverse is an approved correction to a prior measure in the same metric and object, not an extra reverse-service cost. Original ID must exist and cumulative correction cannot exceed its remaining quantity. Separate positive reverse-processing events can use a generic relations contract: source/original_source/reference_field/original_id_field and quantities/units/same_fields. Relations reject missing originals, identity mismatch and repeated over-return.

Missing optional quantity remains None/status=missing; not_applicable remains None; explicit zero retains0 and original evidence. Required missing quantities fail. Zero aggregate driver denominator fails. Source hashes are checked when declared; original bundle manifest protection remains necessary.

## Scope

The fixture proves structured source-export conversion for these declared schemas and approved units. It does not claim arbitrary vendor dialects, raw latitude/longitude path estimation, live network integration, regulatory validation, fraud-proof enterprise signatures, unknown historical order lookup, or every accounting scene. Live systems require authorized endpoints and vendor mapping evidence.