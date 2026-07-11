#!/usr/bin/env bash
# BP-00 V2 (d): produce a REAL BP-04 daemon screenshot inside pmx-sandbox:base
# under gVisor — the exact production rendering path (Debian fonts, Chromium,
# 1280x800 daemon viewport). Run on VM-201.
set -euo pipefail

C=bp00-shot
docker rm -f "$C" >/dev/null 2>&1 || true
docker run -d --name "$C" --runtime=runsc pmx-sandbox:base sleep inf >/dev/null

# docker cp into a RUNNING runsc container is unreliable (gofer view lags
# in-sandbox mkdirs) — pipe everything through docker exec instead.
docker exec -i "$C" sh -c 'cat > /tmp/_browser_daemon.py' < /tmp/_browser_daemon.py
docker exec "$C" mkdir -p /workspace/site
docker exec -i "$C" sh -c 'cat > /workspace/site/index.html' < /tmp/bp00-index.html

docker exec -d "$C" sh -c 'cd /workspace/site && python3 -m http.server 8123 --bind 127.0.0.1'
docker exec -d "$C" sh -c 'PMX_WORKSPACE=/workspace python3 /tmp/_browser_daemon.py >/tmp/daemon.log 2>&1'

# wait for the daemon
for i in $(seq 1 60); do
  if docker exec "$C" sh -c 'curl -sf http://127.0.0.1:8901/health >/dev/null' 2>/dev/null; then break; fi
  sleep 1
done

docker exec "$C" sh -c 'curl -s http://127.0.0.1:8901/action -H "Content-Type: application/json" -d "{\"action\":\"navigate\",\"url\":\"http://127.0.0.1:8123/\"}"' > /tmp/bp00-nav.json
python3 - <<'EOF'
import json
d = json.load(open("/tmp/bp00-nav.json"))
print("ok:", d.get("ok"), "| title:", d.get("title"), "| console:", d.get("console"))
print("screenshot_path:", d.get("screenshot_path"))
assert d.get("ok"), d
EOF

SHOT=$(python3 -c "import json; print(json.load(open('/tmp/bp00-nav.json'))['screenshot_path'])")
docker exec "$C" cat "/workspace/$SHOT" > /tmp/bp00-screenshot.png
ls -la /tmp/bp00-screenshot.png
docker rm -f "$C" >/dev/null
echo REMOTE-DONE
