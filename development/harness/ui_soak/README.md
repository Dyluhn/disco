# Disco UI soak

This harness runs Python Playwright inside `disco-sandbox:base` and drives the
deployed frontend exactly as a user does. It does not call either backend
directly: the browser loads the nginx front door from `--base`, and the UI sends
service traffic through `/svc/app` and `/svc/agent`.

## VM 201 / deployed compose stack

Run from a directory that can be mounted at `/work`:

```sh
docker run --rm --network disco_default -e DISCO_PAIRING_TOKEN=... -v <repo-or-dir>:/work disco-sandbox:base python /work/harness/ui_soak/run.py --base http://frontend --iterations 3 --out /work/out
```

`DISCO_PAIRING_TOKEN` is only consumed if the UI displays its pairing prompt.
The equivalent CLI option is `--pairing-token TOKEN`; the CLI option takes
precedence over the environment. If the deployment auto-pairs or the retained
browser session is already valid, the harness continues without a prompt.

For the quick harness-validation path (one Research iteration, no Build, with a
90-second Research completion bound):

```sh
docker run --rm --network disco_default -e DISCO_PAIRING_TOKEN=... -v <repo-or-dir>:/work disco-sandbox:base python /work/harness/ui_soak/run.py --base http://frontend --smoke --out /work/out-smoke
```

## Local development stack

Keep the browser in the sandbox image even on the workstation. With the local
compose stack exposing its nginx front door on the default port `8088`, Linux
host networking reaches that same front door without relying on the broken host
Chromium:

```sh
docker compose up -d
docker run --rm --network host -e DISCO_PAIRING_TOKEN=... -v "$PWD":/work disco-sandbox:base python /work/harness/ui_soak/run.py --base http://127.0.0.1:8088 --iterations 1 --out /work/out/ui-soak-local
```

Use `--smoke` on that command for the 90-second Research-only check. `--headed`
is available when the container has been given a working display; headless is
the default and the normal VM path.

## Behavior and evidence

Each normal iteration opens a fresh page in a shared browser context, pairs if
needed, creates a fresh Research conversation, then creates a fresh Build
conversation. Clicking the UI's **New** control before each mode switch is
intentional: it prevents Disco from resuming an older active Build. The harness
approves every plan-approval affordance it encounters and waits up to 900
seconds for the authoritative UI status `FINISHED`. It then opens the real
Preview iframe and waits for the requested `UI Soak OK` heading before taking
the screenshot.

The output directory is created if necessary and is overwritten/reused by file
name on a later invocation. It contains:

- `ledger.tsv`, with columns `iteration`, `phase`, `verdict`, and `detail`;
- `iteration-NNN-research.png` for the completed answer (or its failure state);
- `iteration-NNN-build-preview.png` for the finished Preview pane (or its
  failure state).

An iteration passes only when the Research final answer has non-empty rendered
text, Build reaches `FINISHED` and renders the requested Preview (normal mode),
and the page records no uncaught `pageerror` or HTTP response with status 500
or higher. Console errors are retained in the browser-signals ledger detail for
diagnosis but are not an additional PASS criterion. Phase timeouts write a
`FAIL` row instead of hanging. All requested iterations run, and the process
exits nonzero if any iteration fails.

Selector definitions and their exact frontend source files are centralized in
`selectors.py`. The only text-derived selector is the Preview tab's exact
accessible name because that Radix trigger has no `data-disco-control` or test
hook; pairing, navigation, modes, send, answer completion, plan approval, Build
status, and preview frames all use product-defined hooks or semantic attributes.
