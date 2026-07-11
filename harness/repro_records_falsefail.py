"""Reproduce the records-app false-FAIL chain against today's code.

Builds a records app the way app_create does (prepare hook + generate), then
runs the verifier's fallback bundle (lead_gen_verify) exactly as
verify_appkit_app dispatches it for records (verify=None -> lead_gen_verify).
Prints every check verdict + the lead entity the bundle resolved.
"""

from disco.core.appkit import resolve_primitive
from disco.core.appkit.generator import resolve_lead_entity
from disco.core.appkit.primitive_verify import lead_gen_verify
from disco.core.appkit.recipes import RECIPES
from disco.core.appkit.spec import AppSpec, Entity, EntityField, Page, Section

prim = resolve_primitive("records")

# A shift-tracker records app, shaped like the wave-4 hand-written spec:
# one real entity ("shift"), no submit primary action, no entity id "lead".
app = AppSpec(
    schema_version=1,
    name="Shift Tracker",
    app_kind="records",
    entities=[
        Entity(
            id="shift",
            name="Shift",
            fields=[
                EntityField(name="worker", type="text", required=True),
                EntityField(name="start_time", type="text", required=True),
                EntityField(name="end_time", type="text", required=False),
                EntityField(name="notes", type="text", required=False),
            ],
        )
    ],
    pages=[
        Page(
            id="home",
            route="/",
            title="Shifts",
            sections=[Section(id="s1", kind="hero", content={"heading": "Shifts"})],
        )
    ],
    primary_actions=[],
)

app = prim.prepare_app_spec(app) if prim.prepare_app_spec else app
design = RECIPES[0].to_design_spec()
tree = prim.generate(app, design)

lead = resolve_lead_entity(app)
print(f"resolved lead entity: id={lead.id!r} fields={[f.name for f in lead.fields]}")
print(f"schema.sql tables: {[l for l in tree['schema.sql'].splitlines() if 'CREATE TABLE' in l]}")
print(f"worker routes mentioning /api: "
      f"{sorted(set(w.strip()[:60] for w in tree['worker/index.ts'].splitlines() if '/api/' in w))[:6]}")
print()

result = lead_gen_verify(app, design, tree)
for c in result.checks:
    print(f"{'PASS' if c.passed else 'FAIL':4}  {c.name:24} {c.evidence[:140]}")
