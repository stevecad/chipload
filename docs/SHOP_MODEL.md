# Shop model and executable examples

Chipload's current implementation describes and evaluates the shop itself. A shop
project uses ordinary Python to compose resources, process offers, input catalogs,
constraints, evidence, workflows, observations, and business variables. The
compiled result is data served through the shared REST API and MCP operations.

The Python SDK and model are experimental. A local authoring/publication service
and restricted container builds are implemented; see [AUTHORING.md](AUTHORING.md).
REST/MCP and Keycloak token verification now expose the model; see
[API.md](API.md). Production deployment qualification remains open.
RFQ intake and processing are outside the current stage.

## Run the examples

Install Python 3.12 or newer and [uv](https://docs.astral.sh/uv/). From the checkout:

```sh
uv sync --locked
uv run python -m examples.reference_shop
uv run python -m examples.reference_shop --public
uv run python -m examples.heat_treatment
uv run python -m examples.heat_treatment --public
uv run --extra server python -m unittest discover -s tests -v
```

The first example command builds a trusted local Python example and evaluates
sixteen manufacturing questions with explicit expected answers. `--public` prints
the catalog projection. `--artifact` prints the complete private artifact and
must not be used as a public response.

The heat-treatment example runs nine scenarios through the same compiler and
evaluator. It has furnaces, cooling, and batch verification without machining.
Its `--public` option prints the public catalog.

The [process/business library](PROCESS_EXAMPLES.md) adds 88 process projects and
one mixed shop, with 2,147 cases. Each combines technical offers with explicit
prototype/production acceptance conditions and contextual business policies.

`uv run python -m examples.alternative_routes` exercises 24 alternative-route
cases across machining, heat treatment, and a document review service. Each
shop publishes one offer with separate technical and business conditions for
small lots and contracted work.

Only run source you trust in this local environment. The example runner is
ordinary Python execution; it is not an isolated build service.

## A shop project

The [reference project](../examples/reference_shop/shop.py) imports reusable
components from [components.py](../examples/reference_shop/components.py).
Functions and loops create the model; there is no special Python interpreter.
For example, two milling capabilities reuse the same component function with
different machines, fixtures, and envelopes.

The project contains two mills, a shared cutter, fixtures, stock, a deburr bench,
inspection, and an outsourced finishing service. Its acceptance scenarios are
in [scenarios.py](../examples/reference_shop/scenarios.py). All values and evidence
in that example are fictional and explicitly simulated.

The [Swiss shop](../examples/swiss_profile.py) contains shop variables, process descriptions,
and business policies. Its missing machine inventory, site information,
specific alloy qualifications, and tolerances remain missing.

## An abstract engine

There are no built-in shop types. Resource kinds, process names, delivery modes,
input vocabularies, variable categories, and state properties are shop-authored.
Resources and physical sites are optional; a resource only needs a valid site
reference when its author supplies one.

`Catalog` and `CatalogItem` define reusable vocabularies. A `Choice` can bind its
allowed values to a catalog, so compilation checks those references. Materials
are one possible catalog; cooling media or document formats use the same
mechanism. The evaluator accepts named facts, not a mandatory material argument.
Unbound choices also support simple text or boolean requirements.

The [heat-treatment project](../examples/heat_treatment.py) defines a thermal
cycle, controlled cooling, and verification. Its inputs include alloy, incoming
condition, approved cycle/load plan, batch mass, load width, temperature, and
duration. A workflow requires cooling before verification. Shop variables
describe available records and an unknown customer-specific qualification.
All equipment, limits, and evidence are fictional simulation data.

That example demonstrates how a heat-treatment-only shop can describe itself,
not a validated metallurgical model. Actual recipes, relationships between
variables, achieved properties, and qualification scopes must come from the
shop. Passing independent ranges cannot establish a hardness or other outcome
that has not been modeled. The example explicitly tests that such a request
returns `unknown`.

[Abstraction tests](../tests/test_abstract_engine.py) also compile and evaluate
a document-review service with no material, machine, or physical site.
Adding a new shop type must not require branches in the core evaluator.

## Shop variables and business policies

`ShopVariable` records a named shop characteristic or policy with:

- A stable ID, name, category, and explicit visibility.
- A value: text, boolean, quantity, interval, set, or unknown.
- Certainty: declared, approximate, typical, or unknown.
- Source, recording time, and optional expiry.
- Optional applicability predicates and explanatory notes.

For example:

```python
from datetime import datetime, timezone
from chipload import Choice, ShopVariable

monthly_releases = ShopVariable(
    id="monthly-releases",
    name="Monthly release schedules",
    category="production-planning",
    value=True,
    certainty="declared",
    source="person",
    recorded_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
    visibility="public",
    applies_when=(Choice(fact="engagement", allowed=("annual-contract",)),),
    notes="An offered arrangement; not a requirement for every customer.",
)
```

These variables can describe accepted and excluded material families, prototype
and batch work, repeat contracts, release schedules, inventory services, and
typical service expectations. Custom categories and variable IDs allow more
shop-specific knowledge. Applicability predicates remain attached to the value;
consumers must not present a conditional statement as unconditional.

`query_variables()` filters by IDs or category and evaluates applicability against
supplied context. It returns the declared definition, `applicable`,
`not-applicable`, or `unknown`, and an effective value only when applicable.
For example, a monthly-release offer conditioned on an annual contract is
applicable for that context, not applicable for a prototype engagement, and
unknown when the engagement is unspecified. A non-applicable offer is not a
global prohibition. Approximate and typical qualifiers remain on the definition;
missing values remain null. Future or expired statements are not current.
Unused context is reported without turning it into a new requirement.

The default query mode uses only public variables and published conditions.
Hidden and nonexistent IDs have the same lookup behavior. Evaluating private
variables through REST or MCP requires the caller's `evaluation:read` grant
for that shop; the helper itself is not an authentication boundary.

The Swiss example preserves the two reported diameter limits: approximately
32 mm and 1.25 inches, which equals 31.75 mm. It does not choose a more permissive
exact limit. Its usual maximum length of 8–9 inches describes a range for the
upper limit, not a minimum allowed part length. Its minimum order quantity and
annual capacity are unknown.

Business variables describe the shop's offer and preferences. A material-family
list does not qualify every grade or condition. A typical quote turnaround does
not create a deadline. The model does not turn these statements into RFQ,
contract, ordering, or inventory-management actions.

ERP functionality is out of scope for Chipload. ERP integration will provide
timestamped observations and external record references through adapters and
APIs. The ERP remains authoritative for orders, purchasing, inventory management,
production planning, accounting, and invoicing. Chipload uses relevant observations
to evaluate capability and business rules; its model does not manage those records.

## Capability evaluation

A `Capability` combines a process, resource IDs, input requirements,
evidence, required observations, and input/output states. Requirements
currently support exact choices and inclusive numeric ranges with units.
Shop Python can construct and reuse these rules across components.
Ranges can additionally specify a positive, dimensionally compatible
`multiple_of` quantity. Values must be exact multiples measured from zero;
the lower bound does not shift the increments. For whole-item lots, use
`multiple_of=quantity("1", "1")`. This also supports discrete length increments
across equivalent units. Absolute temperatures cannot be used as increments.
An invalid increment produces `invalid-increment`; missing values remain unknown.
Omitting this optional field preserves existing draft artifact encodings.

`evaluate()` operates on compiled data. It never imports source, calls equipment,
fetches URLs, or uses an LLM. Its request identifies a capability, workflow, or offer,
facts, initial state, purpose, and minimum evidence level.
The caller supplies an explicit evaluation time and an observation snapshot.
The REST/MCP service uses its own clock and stored observation snapshot for live
queries; callers cannot override them.

| Outcome | Meaning |
| --- | --- |
| `supported` | The request satisfies the modeled constraints and applicable evidence at the stated time |
| `unsupported` | A known constraint or process incompatibility rejects this request |
| `conditional` | A named setup, scheduling, or other remedy is needed |
| `unknown` | Required facts, evidence, observations, or modeled requirements are missing or insufficient |

For a combined workflow, a known failure makes the whole route unsupported.
Otherwise unknown knowledge takes precedence over conditional remedies; a route
is supported only when all modeled requirements are satisfied. Reasons remain
available for every evaluated step.

A conditional fixture change appears as an explicit planned assumption. The
request is not mutated and the result remains conditional. Extra request facts
that the model does not understand produce `unknown` rather than being ignored.

Workflows propagate state along their declared dependencies. The example route
machines, deburrs, inspects, then anodizes a part. Reordering finishing before its
bare-part inspection fails because the inspection's input state is incompatible.
Conflicting states from different predecessors also fail.

State propagation currently models explicitly declared categorical transitions
for one subject, such as a part or batch. It does not calculate geometry changes, material removal,
tolerance stacks, branching workpiece quantities, or a feasible shop schedule.
Shared tooling appears as a dependency; a readiness observation does not reserve
it or solve concurrent allocation.

## Alternative routes

An `Offer` names a service that can be provided through different capabilities
or workflows. Each route is assessed independently, including its business rules,
evidence, observations and workflow state. For example:

```python
from chipload import Choice, Offer, Route

service = Offer(
    id="service",
    name="Shop service",
    visibility="public",
    alternatives=(
        Route(target_kind="workflow", target="small-lot-route"),
        Route(target_kind="workflow", target="contract-route"),
    ),
    requirements=(Choice(fact="terms-accepted", allowed=(True,)),),
)
# Include service in Shop(..., offers=(service,)), alongside the named workflows.
```

Use `EvaluationRequest(target_kind="offer", target="service", ...)` to evaluate
the offer. Any supported route establishes support subject to the offer's shared
conditions. Otherwise an explicitly conditional route takes precedence over an
unknown route, and an unknown route takes precedence over rejection. The offer
is unsupported only when every route rejects the inputs or a shared condition
rejects them. Missing evidence never becomes rejection merely because another
route is unsupported.

Each alternative's outcome describes that route before the shared conditions;
the aggregate outcome answers the complete offer query. Shared conditions are
checked once, and overlapping route conditions still apply. Extra request facts
remain unknown unless the route or shared conditions model them. Evidence and
state from different alternatives are never combined to create a feasible route.

Results include an `alternatives` assessment list. A supported or conditional
offer also supplies `matching_alternative`; ties follow declaration order.
Authorized private results contain each route's decisions and steps. Public
results contain public route references, outcomes and simulation flags, with
the usual safe explanations and freshness summary. Hidden alternatives, private
workflow steps, or hidden catalog conditions make public evaluation unknown
without evaluating the concealed conditions.

Shared inputs remain available to each route's evidence checks. Unknown request
fields produce one summary per alternative, keeping diagnostics bounded when
many routes are evaluated; they still prevent a claim of support.

A match reports evaluated support. Equipment operations still require their
own concrete capability/workflow checks, grants and adapter bindings. An offer
does not select an adapter, reserve capacity, or produce a schedule. An offer may
reference up to 32 distinct capabilities/workflows; offers cannot reference
other offers. Admission charges the sum of all alternative work and result
sizes, including repeated workflow steps. Existing artifact encodings and public
identities remain unchanged when no offers are present.

See [alternative_routes.py](../examples/alternative_routes.py) for reusable
Python factories, business conditions and source-project exports. This adds
alternative composition to the compiled model; custom Python evaluation of
queries and more general process transformations remain future work.

## Evidence and observations

Evidence records its assurance level, provenance, capability IDs,
resource scope, input constraints, and validity dates. Material or other input
scope uses those constraints, including catalog identities. Declared evidence cannot
satisfy a request for qualified evidence. Expanding a capability does not expand
an existing evidence record. The evaluator requires evidence that covers the
request and all modeled requirement fields and resources.

These are assertions recorded by the shop; the SDK cannot physically verify a
declaration, inspection method, or certificate. Signatures, independent
verification, external revocation feeds, and finer scopes remain design work.

An authorized shop agent can immediately revoke an evidence ID through
`shop.revoke_evidence(...)`; see [the API contract](API.md#evidence-revocation).
Revocations are durable shop records. The evaluator excludes those IDs from
every capability, workflow and offer, including after source rollback or
republication. Other independently applicable evidence can still establish
support. Without it, evidence contributes an unknown outcome; an independently
rejecting constraint can still make the overall result unsupported. Revoking
evidence does not establish that the underlying process is physically impossible.

An evidence ID stays revoked across all revisions, even if its declaration is
edited. A newly established qualification needs a new evidence ID and published
source. The shop still owns its declarations; revocation is not independent
certification. Local artifact evaluation and build acceptance cases have no
live database context; direct evaluator users supply `revoked_evidence` when
reproducing a recorded snapshot. Hosted queries and operation checks always use
the repository's current revocations. Publication, observations and revocations
are selected in one database read snapshot.

Simulated evidence is usable only when a request explicitly permits simulation,
and the result identifies it as simulated.

Observations carry resource/property identity, value, time, maximum age, and
source. Missing, mismatched, future, expired, or unknown values cannot establish
current support. Expiry is exclusive: an observation is stale at its expiry
instant.

Capability and readiness queries have distinct meanings. A busy mill can still
support a manufacturing capability. A readiness query additionally checks
availability and stock. Neither result reserves equipment, material, or time.

## Units, identities, and artifacts

Quantity values are finite decimal strings. Comparison uses exact rational unit
conversion, including 1 inch = 25.4 mm, independent of floating-point rounding or
the process's decimal precision setting. The supported UCUM subset is explicit
in `model.py`; unknown units are rejected. Every quantity requires an explicit
unit. Absolute temperatures support `K`, `Cel`, and `[degF]`, with exact offsets
as well as scale conversion. For example, 700 °C equals 1292 °F at a range
boundary. Temperature differences and ramp rates are not implemented as
absolute temperatures. Unit semantics follow the supported subset of
[UCUM](https://ucum.org/ucum), not the entire standard.

Collections use permanent IDs. Compilation rejects duplicate IDs, broken
references, cycles, cross-site resource parents, and ambiguous fact definitions.
Cross-object diagnostics include repair paths. Pydantic errors identify invalid
fields, including unknown fields and incompatible bounds.

The draft runtime now enforces model admission limits. `sdk_reference()` exposes
them to agents through JSON Schema and a `limits` object:

| Collection or value | Maximum |
| --- | --- |
| Each top-level shop collection; items in each catalog | 1,000 |
| Facts or rules in one collection | 100 |
| Resource references or evidence capability references | 256 |
| Evidence references on a capability | 32 |
| Observation requirements on a capability | 64 |
| Workflow steps; predecessors per step | 256; 64 |
| Alternatives in one offer | 32 |
| Choice values or descriptive set values | 256 |
| Text values, descriptions, and remedies | 4,000 characters |
| Workflow state, including a reserve for request fields | 512 fields |
| Cross-object diagnostics returned per validation | 100 |

Compilation also checks deterministic work and result-size scores in
`src/chipload/complexity.py`. Each capability and workflow has a 100,000 work
budget and a 256,000 result-weight budget. Repeated steps count repeatedly;
evidence checks, choice comparisons, state joins, and repeated decision output
contribute to the budgets. Combined variable applicability has the same limits.
These scores are admission policies, not milliseconds or exact serialized byte
counts. Exceeding them rejects a build with a `model-complexity` diagnostic.
It does not change a manufacturing result to `unsupported` or silently omit work.
Large definitions must be reorganized before publication.

The data decoder checks nesting (32 levels) and scan tokens (100,000) before
allocating JSON objects. Scan tokens count punctuation outside strings, string
openings, and escapes; punctuation inside ordinary strings does not count.
Compiled models reserve envelope space with a 90,000-token limit. Byte limits
still apply. Resource and workflow graph ordering visits each dependency edge
once, with sorting for deterministic layers. Valid existing models keep their
canonical representation and digests; definitions exceeding the new limits
are no longer admissible, including when reading older stored artifacts.

`compile_shop()` accepts a constructed model and build identity. It produces a
model digest and a separate build digest that includes source, dependency lock,
SDK source, and Python runtime identity. `read_artifact()` checks the data,
references, size, duplicate JSON keys, and digests. Digests detect changes; they
do not authenticate who built an artifact. Trusted build infrastructure must establish
build provenance and enforce reproducibility and resource limits. The local
Docker broker now constructs this identity from its source bundle and pinned
worker environment, revalidating the model outside the source process.

The SDK uses frozen objects and tuples. Pydantic's
[model validation and JSON schema support](https://docs.pydantic.dev/latest/concepts/models/)
provide the structural schema; Chipload adds cross-object and domain validation.

## Publication and privacy

Everything is private by default. `public_catalog()` is an explicit allowlist
projection of the selected shop model. It includes opted-in metadata and shop
variables and omits private resources, source/build provenance, equipment
addresses, secret references, observation identifiers, and evidence internals.
Private reference targets are omitted from public reference lists.

Input catalogs include only public catalogs and public items. A catalog-bound
choice whose allowed values are partially or entirely hidden has
`complete: false`; an empty redacted list does not mean that nothing is allowed.
Public variable queries return unknown for such conditions without testing
whether a guessed value matches a hidden item. The public projection is a
discovery view, not an executable replacement for the private artifact.

A public capability publishes its declared requirements. A public variable
publishes its value, notes, provenance, and applicability. Authors must ensure
these deliberately public fields contain only publishable information.

The public projection has its own digest. Editing a private controller address
does not change the public catalog or its digest. The local lifecycle service
stores exact publications and enforces application grants; callers must not
treat this projection helper as an authorization system. Internal evaluation
results can contain private references. The REST/MCP service returns a restricted
public evaluation response; detailed evaluation requires the caller's
`evaluation:read` grant for that shop. See [API.md](API.md) for the response and
authorization contracts.

## Callable operation contracts

`Shop.operations` adds `ShopOperation`: named typed inputs/outputs, an explicit
effect description, logical adapter/command, resource references, prerequisite
capability/workflow checks, and a dispatch timeout. Fields support text, boolean,
and explicitly unit-bearing quantity values. A call requires supported technical
and business checks using current observations. Shop and process kinds remain
open vocabulary; operations introduce no manufacturing-specific engine branches.

Contracts are authored in Python and compiled as data. Public operation discovery
exposes only opted-in descriptions and field contracts. Operator-owned bindings
and execution permissions separately determine whether a call can run. See
[OPERATIONS.md](OPERATIONS.md) for the execution service and simulator.

Bounds are 1,000 operations per shop, 100 input/output fields each, 64 resources,
16 checks, and 1–300 timeout seconds. Combined checks share the evaluation work
and result budgets; resource references must cover every checked capability.
Operations are private by default. Empty operation collections are omitted from
canonical model encoding to preserve existing artifact identities. Public catalog
responses now include an `operations` collection, changing their representation
digest even when it is empty.

## Version and migration decision

The original `SPEC.md` continues to specify v0.1 YAML/JSON documents. This Python
model uses the separate `0.2-draft` schema and the SDK version `0.2.0.dev0`.
It does not silently reinterpret or import v0.1 files.

Permanent IDs, explicit units, timestamps, provenance, freshness, and honest
unknown values carry forward. In the draft model, a preliminary shop profile
can omit sites and resources; a resource's site is optional, and a supplied
reference must resolve. Descriptive business variables can preserve uncertainty without
creating executable manufacturing claims.

Legacy report/action capability names such as `shop.status@1` keep their original
meaning. The new manufacturing offers have IDs scoped to a shop model revision.
A future migration must map legacy capabilities explicitly and must not interpret
`shop.jobs@1` as proof of machinability, permission to execute, or a transport.
Draft schema changes must update examples, tests, and this decision together.

The unreleased draft's dedicated `materials` collection and evaluation
`material` argument have been replaced by generic catalogs and named facts.
Resource kinds are open vocabulary and units no longer have an implicit default.
Rebuild older experimental artifacts from updated source; there is no released
compatibility promise or automatic migration between these draft iterations.
