# Trusted-component registry data

Layout (spec §1.1): `<name>/<X.Y.Z>/manifest.json` + `core/` (immutable,
hash-pinned — regenerate pins with `scripts/component_pins.py`, never by hand)
+ `config/` (copied once as defaults, then the user's) + `GUIDE.md` +
`probe/probe.py` (host-run only; never installed into a workspace).

New versions are new sibling directories. Keep old versions: dropping one
flips installed apps to `ejected(registry-version-missing)` at verify (D11).
