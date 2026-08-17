#!/usr/bin/env bash
# Starter-catalog live proof on the deployed VM 201 stack: start a game build,
# then poll the event log for a scaffold_starter call + kit files on disk.
set -eu
B="http://localhost:8088"
O="Origin: http://localhost:8088"
J=/tmp/proof3.jar

TOK=$(sudo docker compose -f /opt/disco-p5/compose.yaml logs agent-server 2>/dev/null \
  | grep -oE "pairing token[^:]*: [A-Za-z0-9_-]+" | tail -1 | grep -oE "[A-Za-z0-9_-]{40,}")
CSRF=$(curl -s -c $J -X POST $B/svc/app/api/auth/mint -H "$O" \
  -H "Content-Type: application/json" -d "{\"pairing_token\":\"$TOK\"}" \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['csrf_token'])")
CID=$(curl -s -b $J -H "$O" -H "X-Disco-CSRF: $CSRF" -H "Content-Type: application/json" \
  -X POST $B/svc/agent/conversations -d '{"surface":"build"}' \
  | python3 -c "import sys,json;print(json.load(sys.stdin)['conversation_id'])")
echo "CID: $CID"
curl -s -b $J -H "$O" -H "X-Disco-CSRF: $CSRF" -H "Content-Type: application/json" \
  -X PATCH $B/svc/agent/conversations/$CID/settings -d '{"autonomous": true}' > /dev/null
curl -s -b $J -H "$O" -H "X-Disco-CSRF: $CSRF" -H "Content-Type: application/json" \
  -X POST $B/svc/agent/conversations/$CID/messages \
  -d '{"content": "build a simple breakout-style browser game — paddle, ball, bricks, score", "build_brief": {}}' > /dev/null
echo "$CID" > /tmp/proof3.cid
echo "build started"

# Poll up to ~12 min for a scaffold_starter RESULT or a terminal status.
for i in $(seq 1 72); do
  R=$(sudo docker exec disco-app-server-1 python -c "
import sqlite3, json
c = sqlite3.connect('/data/disco.db'); c.row_factory = sqlite3.Row
rows = list(c.execute('SELECT seq, kind, payload FROM events WHERE conversation_id=? ORDER BY seq', ('$CID',)))
for r in rows:
    p = json.loads(r['payload'])
    tc = p.get('tool_call') or {}
    tr = p.get('tool_result') or {}
    if tc.get('tool_name') == 'scaffold_starter':
        print('CALL', r['seq'], json.dumps(tc.get('arguments')))
    if tr.get('tool_name') == 'scaffold_starter':
        print('RESULT', r['seq'], 'success=', tr.get('success'), str(tr.get('content'))[:160].replace(chr(10),' | '))
    if r['kind'] == 'status' and p.get('status') in ('FINISHED','ERROR','STUCK'):
        print('TERMINAL', p.get('status'), p.get('detail') or '')
" 2>/dev/null)
  if echo "$R" | grep -q "RESULT\|TERMINAL"; then echo "$R"; exit 0; fi
  sleep 10
done
echo "TIMEOUT"; echo "$R"
