# platform/

The **platform-owned** half of this repository — the business, tenant and control layer built
around the upstream Hermes Agent runtime.

Nothing here is imported by upstream code. Nothing here may be imported *into* upstream code.

**Start with [`AGENTS.md`](AGENTS.md)** for the working rules, and
[`../docs/platform/ARCHITECTURE_BOUNDARIES.md`](../docs/platform/ARCHITECTURE_BOUNDARIES.md) for
the authoritative boundary definition.

| Document | What it covers |
|---|---|
| `docs/platform/ARCHITECTURE_BOUNDARIES.md` | The upstream/platform line, protected identifiers, control-plane and deployment boundaries, guardrails |
| `docs/platform/IMPLEMENTATION_PLAN.md` | Extension points, module layout, identity layer, dashboard/runtime communication, AWS boundary, tenant config, Phase 1 |
| `docs/platform/CORE_PATCHES.md` | The patch budget — every upstream file touched, and why |
| `docs/platform/HERMES_PLATFORM_AUDIT.md` | Phase 0 assessment of the existing runtime |
| `docs/platform/BRAND_SURFACE_AUDIT.md` | All 101,368 product-name occurrences, classified |

## Planned layout

Directories appear as their phase lands; this is the intended shape, not the current contents.

```
identity/       Branding config + projectors (skin, locale, persona, theme tokens)
config/         Typed tenant schema — organization, agents, integrations,
                knowledge, permissions, limits
agents/         AgentSpec → profile materialiser
policy/         (agent, tool, action) → allow | require-approval | deny
control/        Platform Control API — read models + narrow typed commands
supervisor/     Decompose → route → Kanban → collect
knowledge/      Ingest → extract → chunk → index → retrieve
observability/  JSON logs, correlation IDs, CloudWatch
budget/         Token and cost ceilings
cli/            `nova` — platform and deployment operations only
```

## Three rules, in short

1. **The dependency arrow points one way.** Platform imports Hermes; never the reverse.
2. **Branding is compiled into existing surfaces**, never hard-coded and never read from core.
3. **Add names beside upstream ones; never replace them.**
