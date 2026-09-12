# Troubleshooting

Start with the doctor. It runs inside the agent-server container, checks each thing
that can be wrong, and prints one line per check with the real reason:

```bash
podman compose exec agent-server python -m disco.agent_server.doctor
# docker compose exec agent-server python -m disco.agent_server.doctor
```

```
PASS config        build v0.2.0 (3f9c1a2) (image); 14 DISCO_* variables set
PASS agent-server  ok, version v0.2.0 (3f9c1a2)
PASS app-server    ok, version v0.2.0 (3f9c1a2)
FAIL sandbox       podman: podman sandbox host unix:///var/run/docker.sock unreachable: [Errno 2] No such file or directory
PASS data disk     41.2 GB free at /data
PASS database      /data/disco.db: quick_check ok, 18342 events, 61 MB
PASS secret key    DISCO_SECRET_KEY set
PASS driver model  config PASS | completion PASS | tool-calling PASS

1 check(s) FAILED
```

The process exits non-zero when a check fails, so it works in a script too. `--no-model`
skips the driver-model check (it calls your model once). `--app-url` points the
app-server check somewhere other than `http://app-server:8800` (a split deployment).

## Reporting a problem

```bash
podman compose exec agent-server python -m disco.agent_server.doctor --bundle /tmp/disco-doctor.json
podman compose cp agent-server:/tmp/disco-doctor.json .
```

Open a [bug report](https://github.com/Dyluhn/disco/issues/new/choose), paste the
doctor table, and attach `disco-doctor.json`. The bundle contains the build identity,
the checks, your `DISCO_*` settings with secret values replaced by `***REDACTED***`,
both health bodies, the sandbox probe, and the shape of your last ten conversations
(event kinds, tool names, exit codes and error text — never prompts, files or model
output). Add the window of `podman compose logs agent-server` around the failure.

Security problems go to the [security policy](../SECURITY.md), not a public issue.

## Reading logs

```bash
podman compose logs -f agent-server      # the loop, providers, sandbox
podman compose logs -f app-server        # settings, auth, pairing, quotas
podman compose logs --since 10m frontend
```

`DISCO_LOG_LEVEL=DEBUG` in `.env` raises both Python servers; `DISCO_LOG_JSON=1`
emits one JSON object per line. Container logs are bounded at 20 MB per service
(`compose.yaml`, `logging:`); they are gone after `compose down`, so copy what you need first.

## Common failures, by message

| You see | Cause | Fix |
|---|---|---|
| `podman sandbox host unix:///var/run/docker.sock unreachable` (banner, doctor, Settings → Runtime) | The rootless Podman socket is not enabled or not mounted | `systemctl --user enable --now podman.socket`, then `export DISCO_SANDBOX_SOCKET=$XDG_RUNTIME_DIR/podman/podman.sock` and `podman compose up -d` again |
| `image "disco-sandbox:base" is not present on the host; this backend never pulls` | The `sandbox-image` service did not run or was commented out | `podman compose up -d sandbox-image`; for a source build `podman compose -f compose.yaml -f compose.build.yaml up -d --build` |
| `Error: invalid port format - format is [[hostIP:]hostPort:]containerPort` | `podman-compose` 1.3.x cannot expand `${VAR:-default}` for a service's own keys | Install `docker-compose` (Compose v2) as the provider; see [self-host.md](./self-host.md#prerequisites) |
| Settings → Models: the provider Test fails with a connection error to `host.docker.internal` | Rootless Docker resolves that name to an address with no route back to the host | Use the host's LAN IP and bind the model server to `0.0.0.0`; table in [self-host.md](./self-host.md#reaching-a-service-running-on-the-host-machine) |
| The browser asks for a pairing token | The UI is not being opened from the same machine, or a proxy sits in front | `podman compose exec app-server python -m disco.app_server.pairing_cli` prints it |
| A Build runs but never uses tools, or "tool-calling FAIL" in the doctor | The driver model cannot emit tool calls | Pick a model that does (see Models in self-host.md); the doctor's driver-model line quotes the provider's error |
| `agent-server` health `degraded` with `store: error` | The database file is unreadable or the volume is full | Doctor's data-disk and database lines say which; free space or restore a backup ([self-host.md](./self-host.md#backup-and-restore)) |
| Everything is healthy but a run sits at `RUNNING` for a long time | The model is slow or the run is verifying repeatedly | The Checks section of the run shows what it re-ran; `DISCO_INSPECT=1` and `/api/debug/trace/<id>` give per-turn model timing |
| Settings say a key is saved but every run fails auth | The encrypted settings key changed between restarts | Set `DISCO_SECRET_KEY` (the doctor warns when none is set); rotation procedure in [self-host.md](./self-host.md#rotate-the-encrypted-settings-key) |
| `disco-verify` says every check FAILED at once | It was run with `compose exec`, which skips the entrypoint, on a build before the secret fix | Update; current builds resolve the key themselves |

## Which build am I running?

```bash
curl -s http://127.0.0.1:8000/health | python3 -m json.tool      # agent-server
curl -s http://127.0.0.1:8800/api/health | python3 -m json.tool  # app-server
```

Both return `version` (tag and commit) and `build`. Images from a release carry the
tag and commit; a local `compose.build.yaml` build says `unknown` unless you export
`DISCO_BUILD_TAG` and `DISCO_BUILD_COMMIT=$(git rev-parse --short HEAD)` first.

## Deeper: per-conversation trace

With `DISCO_INSPECT=1` in `.env` (restart the agent-server), `GET /api/debug/trace/<conversation id>`
returns routing decisions, per-step model timing and tool scopes, and
`GET /api/debug/evidence/<conversation id>` the full redacted event log for one run.
Both are owner-scoped; the doctor bundle is the lighter export for a bug report.
