# Phase 1 — what was built

The platform layer's first working slice: a declarative agent specification, a
runtime-agnostic adapter contract, a Hermes adapter that materializes agents without
touching runtime code, white-label identity, and an audit log that enforces
"model-visible means logged".

**Core patches spent: 1** (a routing-table row in `AGENTS.md`, recorded in
`CORE_PATCHES.md`). Everything else lives in paths upstream has never heard of.

---

## What exists

| Module | Responsibility |
|---|---|
| `nova/spec/agent.py` | `AgentSpec` — one declarative agent. Modelled on the `agent.cordis.yml` concept |
| `nova/spec/identity.py` | `IdentitySpec` — the white-label surface |
| `nova/spec/organization.py` | `OrganizationSpec` — tenant identity |
| `nova/spec/bundle.py` | `TenantBundle` — a directory loaded and cross-checked as one unit |
| `nova/_fields.py` | Typed field extraction, env interpolation, unknown-key rejection |
| `nova/runtime/base.py` | `AgentRuntime` contract, expressed only in NOVA vocabulary |
| `nova/runtime/registry.py` | Adapter lookup by name; adapters imported lazily |
| `nova/runtime/hermes/` | The Hermes adapter — paths, materializer, skin projector |
| `nova/audit/log.py` | Append-only JSONL log with write-ahead model-visible changes |
| `nova/apply.py` | Bundle → runtime orchestration |
| `nova/cli.py` | `python -m nova validate \| plan \| apply \| status` |
| `scripts/check_protected_identifiers.py` | CI guardrail for the boundary rules |

Dependencies: **the standard library and PyYAML**. Nothing else. The platform layer
imports and its tests run without the runtime installed.

## How to use it

```bash
python -m nova validate nova/examples/acme    # load and fully validate a bundle
python -m nova plan     nova/examples/acme    # show what applying would change
python -m nova apply    nova/examples/acme    # make the runtime match the bundle
python -m nova status                         # what the runtime currently holds
```

`nova/examples/acme` is a complete two-agent tenant bundle used by the tests.

## Architectural decisions

**The package is `nova/`, not `platform/`.** A top-level `platform/` package shadows the
standard library module that 40 upstream modules import for OS detection. Verified
empirically before any code was written; enforced by the guardrail.

**No validation library.** Hand-written field extraction keeps the dependency surface at
PyYAML, names the customer's own field paths in errors, and rejects unknown keys — so a
misspelled key fails at load instead of silently producing an agent that lacks the
setting.

**Branding is compiled, not looked up.** `IdentitySpec` is projected into a skin file the
runtime already loads ahead of its built-ins. Rebranding is a config change plus a
re-apply; no runtime code participates.

**The skin projector lives in the adapter**, not in a shared identity package: the skin
schema belongs to the runtime. What is shared is `IdentitySpec`, which is in NOVA's terms.

**Model-visible changes are write-ahead logged.** `AuditLog.model_visible_change()` writes
`intent`, runs the change, then writes `committed` or `failed`. `record()` refuses
model-visible kinds, so the invariant is structural rather than a convention. A crash
leaves an open intent, which `open_intents()` surfaces and the CLI warns about.

**NOVA writes only what it owns.** Every materialized profile carries `nova-agent.json`.
A profile without one is refused, so a hand-built agent is never silently overwritten.
Credentials, session history, memories and databases are on a never-write list, and
removal refuses when customer state is present.

**Idempotence via spec digest.** The digest covers meaning, not source path. Re-applying
an unchanged bundle writes nothing; a deleted file is still restored despite a current
marker.

## Known limitations

These are recorded rather than hidden. Each is a deliberate Phase 1 boundary.

- **Positive tool scoping is not enforced.** `tools.deny` compiles to the runtime's
  unconditional deny list and *is* enforced ahead of any bypass. `tools.toolsets` and
  `tools.allow` are recorded under the `nova:` config key and warned about, because the
  runtime resolves toolsets from `platform_toolsets[<surface>]` and writing a narrowed
  list there would strip the kanban tools a dispatched worker needs to report completion.
  Enforcing this correctly needs the policy layer.
- **`permissions` and `approval.required_for` are recorded, not enforced.** The policy
  layer is a later phase; the declaration is preserved so bundles authored now keep
  working when enforcement arrives underneath them.
- **`knowledge.sources` is inert.** No runtime can retrieve yet. Declaring sources
  produces a warning, never a silent drop.
- **No Control API and no dashboard.** Deferred from the original Phase 1 scope.
- **No environment-variable or home-directory aliases in the runtime itself.** NOVA
  resolves `$NOVA_HOME → ~/.nova → $HERMES_HOME → ~/.hermes` for its own purposes; the
  runtime still reads its own variables. Aliasing inside the runtime is a future core patch.
- **`apply` never deletes.** An agent dropped from a bundle produces a warning; removal
  stays an explicit operator action.
- **Single tenant per deployment.** The tenant id is carried everywhere, but nothing
  enforces isolation between tenants in one home.

## Extension points

**Add a runtime.** Implement `AgentRuntime` and call `register_runtime(name, factory)`.
Nothing above the adapter changes. `RuntimeCapabilities` is how a new runtime reports what
it cannot do, so the platform degrades honestly instead of assuming.

**Add a spec field.** Add it to the relevant dataclass's `parse` and `to_dict`. Unknown
keys are rejected, so the field must be declared before a bundle may use it. It enters the
digest automatically.

**Add an audited event.** Use `audit.record(...)`. If it changes what a model will see,
add the kind to `MODEL_VISIBLE_KINDS` and write it through `model_visible_change()` — the
test suite fails if a declared kind is not reachable that way.

**Add an identity surface.** Write a projector that consumes `IdentitySpec`. Put it in the
adapter package if its output format belongs to a runtime.

## Tests

`tests/platform/` — 120 tests, runnable with only pytest and PyYAML installed.

| File | Covers |
|---|---|
| `test_spec_agent.py` | Parsing, validation, env interpolation, digests, path escapes |
| `test_spec_bundle.py` | Bundle loading, cross-references, duplicates, malformed YAML |
| `test_audit.py` | The invariant, write-ahead phases, open intents, permissions |
| `test_hermes_materialize.py` | Home resolution, config compilation, idempotence, ownership, protected files |
| `test_identity.py` | Projection, idempotence, rebranding without code change |
| `test_apply.py` | End-to-end apply, ordering, orphans, two tenants one build |
| `test_boundaries.py` | Import direction, dependency surface, vocabulary leakage, stdlib shadowing |
