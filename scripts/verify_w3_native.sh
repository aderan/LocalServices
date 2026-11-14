#!/bin/bash
# W3 原生冒烟（LOCOVE-WORKBENCH-UI）：隔离 daemon + guitest 测试副本 App。
# 流程：构建 → 隔离 daemon 注册 ok/failed 夹具并启动到目标状态 → 启动测试 App
# → AX 验证（左右键与 Control 点按 / 故障服务 / 退出）与真实面板截图。
# 证据：$OUT/w3-menubar-clicks.txt、$OUT/w3-native-summary.txt、$OUT/w3-popover.png
# 用法：bash scripts/verify_w3_native.sh [证据目录]（需 GUI 会话与辅助功能权限）
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/artifacts/acceptance-LOCOVE-WORKBENCH-UI}"
mkdir -p "$OUT"

fail() { echo "FAIL: $*" | tee -a "$OUT/w3-native-summary.txt"; exit 1; }

bash "$REPO/menubar/build.sh" >/dev/null

T="$(mktemp -d /tmp/ls-w3-XXXXXX)"
APP="$T/LocoveMenubarTest.app"
cp -R "$REPO/menubar/build/Locove.app" "$APP"
/usr/libexec/PlistBuddy -c 'Set :CFBundleIdentifier com.mybrain.localservices.menubar.guitest' \
  "$APP/Contents/Info.plist" >/dev/null
/usr/libexec/PlistBuddy -c 'Set :CFBundleName LocoveMenubarTest' "$APP/Contents/Info.plist" >/dev/null
codesign --force --sign - "$APP" >/dev/null 2>&1

PORT=$(python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); print(p)
PY
)
OKPORT=$(python3 - <<'PY'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); print(p)
PY
)
PREFIX="localservices.w3guitest.$RANDOM"
mkdir -p "$T/home" "$T/services" "$T/src"
echo w3 > "$T/src/VERSION"

cleanup() {
  [ -n "${APP_PID:-}" ] && kill "$APP_PID" 2>/dev/null || true
  [ -n "${DAEMON_PID:-}" ] && kill "$DAEMON_PID" 2>/dev/null || true
  sleep 1
  for plist in "$T/home/agents/"*.plist; do
    [ -e "$plist" ] || continue
    launchctl bootout "gui/$(id -u)" "$(basename "$plist" .plist)" 2>/dev/null || true
  done
  rm -rf "$T"
}
trap cleanup EXIT

LOCALSERVICES_HOME="$T/home" LOCALSERVICES_SERVICES="$T/services" \
LOCALSERVICES_PORT="$PORT" LOCALSERVICES_LABEL_PREFIX="$PREFIX" \
LOCALSERVICES_THROTTLE=1 LOCALSERVICES_BACKOFF_SCALE=0.25 \
LOCALSERVICES_HEALTHY_AFTER=4 LOCALSERVICES_MAX_FAILURES=3 \
LOCOVE_ADMIN_PORT=$((PORT + 1)) LOCOVE_PROXY_PORT=47898 LOCOVE_FORCE_REAPPLY=3600 \
  python3 -m localservices.daemon >/dev/null 2>&1 &
DAEMON_PID=$!

for _ in $(seq 1 100); do
  curl -sf --noproxy '*' "http://127.0.0.1:$PORT/api/ping" >/dev/null && break
  sleep 0.1
done
TOKEN="$(cat "$T/home/token")"
API="http://127.0.0.1:$PORT"
H1="X-LocalServices-Token: $TOKEN"
H2="Content-Type: application/json"

register() { # <id> <components-json> <revision>
  curl -sf --noproxy '*' -X POST -H "$H1" -H "$H2" -d '{
    "schemaVersion": 1, "revision": '"$3"',
    "project": {"id": "w3fix", "name": "W3Fixtures", "agent": "none"},
    "directories": [{"path": "'"$T/src"'", "role": "source"}], "by": "verify",
    "service": {"id": "'"$1"'", "name": "'"$1"'", "description": "w3 native fixture",
      "source": "'"$T/src"'", "open": "http://127.0.0.1:'"$OKPORT"'/",
      "evidence": {"agent": "none", "summary": "verify"},
      "components": '"$2"'}}' "$API/api/register"
}
REV=0
register w3-ok "[{\"id\":\"web\",\"kind\":\"server\",\"name\":\"web\",\"cwd\":\".\",
  \"cmd\":[\"python3\",\"$REPO/tests/fixture_server.py\",\"$OKPORT\"],
  \"port\": $OKPORT, \"ready\": {\"http\": \"http://127.0.0.1:$OKPORT/\"}}]" $REV \
  | python3 -c 'import json,sys; print(json.load(sys.stdin).get("revision", 0))' > "$T/rev" || fail "register w3-ok"
REV="$(cat "$T/rev")"
register w3-bad "[{\"id\":\"main\",\"kind\":\"server\",\"name\":\"main\",\"cwd\":\".\",
  \"cmd\":[\"/usr/bin/false\"]}]" $REV >/dev/null || fail "register w3-bad"

curl -sf --noproxy '*' -X POST -H "$H1" -H "$H2" -d '{}' "$API/api/projects/w3-ok/start" >/dev/null
curl -sf --noproxy '*' -X POST -H "$H1" -H "$H2" -d '{}' "$API/api/projects/w3-bad/start" >/dev/null
echo "WAIT 夹具状态收敛…"
python3 - "$PORT" "$TOKEN" <<'PY' || fail "fixtures did not reach available+failed"
import json, sys, time, urllib.request
port, token = sys.argv[1], sys.argv[2]
req = urllib.request.Request(f"http://127.0.0.1:{port}/api/status", headers={"X-LocalServices-Token": token})
deadline = time.time() + 120
while time.time() < deadline:
    d = json.load(urllib.request.urlopen(req))
    st = {p["id"]: p["state"] for p in d.get("projects", [])}
    if st.get("w3-ok") == "available" and st.get("w3-bad") == "failed":
        print("FIXTURES", st); sys.exit(0)
    time.sleep(1)
print("TIMEOUT", st); sys.exit(1)
PY

echo "LAUNCH guitest App（隔离端口 ${PORT}）"
LOCALSERVICES_PORT="$PORT" LOCALSERVICES_HOME="$T/home" \
  "$APP/Contents/MacOS/Locove" >/dev/null 2>&1 &
APP_PID=$!
sleep 5
kill -0 "$APP_PID" 2>/dev/null || fail "test app exited early"

echo "RUN 原生快捷面板"
swift "$REPO/tests/verify_menubar_clicks.swift" "$APP_PID" "$OUT/w3-popover.png" \
  2>&1 | tee "$OUT/w3-menubar-clicks.txt"
echo "PASS: W3 native popover smoke" | tee "$OUT/w3-native-summary.txt"
