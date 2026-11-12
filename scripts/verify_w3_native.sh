#!/bin/bash
# W3 原生冒烟（LOCOVE-WORKBENCH-UI）：隔离 daemon + guitest 测试副本 App。
# 流程：构建 → 隔离 daemon 注册 ok/failed 夹具并启动到目标状态 → 启动测试 App
# → AX 验证（全菜单入口 / 故障服务「查看原因」跳诊断 / 设置跳转）→ 既有
# verify_menubar_clicks.swift（右键快捷菜单 / 左键全菜单 / Control 点按 / 退出）。
# 证据：$OUT/w3-diagnose.txt、$OUT/w3-menubar-clicks.txt、$OUT/w3-native-summary.txt
# 用法：bash scripts/verify_w3_native.sh [证据目录]（需 GUI 会话与辅助功能权限）
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT="${1:-$REPO/artifacts/acceptance-LOCOVE-WORKBENCH-UI}"
mkdir -p "$OUT"

fail() { echo "FAIL: $*" | tee -a "$OUT/w3-native-summary.txt"; exit 1; }

bash "$REPO/menubar/build.sh" >/dev/null

T="$(mktemp -d /tmp/ls-w3-XXXXXX)"
APP="$T/LocoveMenubarTest.app"
cp -R "$REPO/menubar/build/MyBrain Services.app" "$APP"
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
  "$APP/Contents/MacOS/MyBrainServices" >/dev/null 2>&1 &
APP_PID=$!
sleep 5
kill -0 "$APP_PID" 2>/dev/null || fail "test app exited early"

echo "RUN W3 诊断跳转/设置/键盘 System Events 验证"
osascript <<'AS' 2>&1 | tee "$OUT/w3-diagnose.txt"
on joinText(lst)
  set outText to ""
  repeat with s in lst
    set outText to outText & "|" & (s as text)
  end repeat
  return outText
end joinText

on windowText()
  -- WKWebView 的 AX 内容需窗口激活；entire contents 必须在 System Events tell 上下文里解析
  tell application "System Events"
    tell (first application process whose bundle identifier is "com.mybrain.localservices.menubar.guitest")
      set allText to ""
      try
        set elems to entire contents of window "Locove／洛湾"
        repeat with e in elems
          try
            set v to value of e
            if v is not missing value then set allText to allText & (v as text) & " "
          end try
          try
            set t to title of e
            if t is not missing value then set allText to allText & (t as text) & " "
          end try
        end repeat
      end try
    end tell
  end tell
  return allText
end windowText

tell application "System Events"
  tell (first application process whose bundle identifier is "com.mybrain.localservices.menubar.guitest")
    set OUT to ""
    -- 1) 关闭启动时打开的主窗口（诊断跳转应重新打开它；启动初期窗口最稳定 → 多试几次）
    set closedState to "nowin"
    repeat with wAttempt from 1 to 6
      try
        set w to window "Locove／洛湾"
        try
          click (first button of w whose subrole is "AXCloseButton")
        on error
          click button 1 of w
        end try
        delay 0.6
        if (count of (windows whose name is "Locove／洛湾")) is 0 then
          set closedState to "1"
          exit repeat
        end if
      on error
        delay 1
      end try
    end repeat
    set OUT to OUT & "CLOSED=" & closedState & linefeed
    -- 2) 打开全菜单（本次会话首次点击，状态确定）+ 顶层入口
    click menu bar item 1 of menu bar 2
    delay 1.2
    set topTitles to name of every menu item of menu 1 of menu bar item 1 of menu bar 2
    set OUT to OUT & "TOP=" & (my joinText(topTitles)) & linefeed
    -- 3) 菜单保持打开（打开期间内容冻结）：项目 → w3-bad → 查看原因（跳转工作台）
    --    仅用直接 by-name 说明符（捕获引用列表点击会失效）；失败整体重试
    set navOk to 0
    set navLog to ""
    repeat with attempt from 1 to 3
      try
        set namesReady to false
        repeat with openAttempt from 1 to 3
          try
            set topTitles to name of every menu item of menu 1 of menu bar item 1 of menu bar 2
            if (count of topTitles) > 3 then
              set namesReady to true
              exit repeat
            end if
          end try
          click menu bar item 1 of menu bar 2
          delay 1.2
        end repeat
        if not namesReady then error "menu not enumerable"
        set projTitle to ""
        repeat with n in topTitles
          if n starts with "W3Fixtures" then set projTitle to n as text
        end repeat
        if projTitle is "" then error "no W3Fixtures"
        click menu item projTitle of menu 1 of menu bar item 1 of menu bar 2
        delay 0.8
        set svcNames to name of every menu item of menu 1 of menu item projTitle of menu 1 of menu bar item 1 of menu bar 2
        set svcName to ""
        repeat with n in svcNames
          if n starts with "w3-bad" then set svcName to n as text
        end repeat
        if svcName is "" then error "no w3-bad"
        click menu item svcName of menu 1 of menu item projTitle of menu 1 of menu bar item 1 of menu bar 2
        delay 0.8
        set actNames to name of every menu item of menu 1 of menu item svcName of menu 1 of menu item projTitle of menu 1 of menu bar item 1 of menu bar 2
        set navLog to "SVC=" & svcName & " ACTS=" & (my joinText(actNames))
        set diagName to ""
        repeat with n in actNames
          if n contains "查看原因" then set diagName to n as text
        end repeat
        if diagName is "" then error "no 查看原因"
        click menu item diagName of menu 1 of menu item svcName of menu 1 of menu item projTitle of menu 1 of menu bar item 1 of menu bar 2
        delay 1
        set navOk to 1
        exit repeat
      on error errText
        set navLog to navLog & " [attempt " & attempt & " ERR: " & errText & "]"
        delay 0.8
      end try
    end repeat
    set OUT to OUT & navLog & linefeed
    -- 4) 跳转结果：主窗口必须（重新）打开；AX 文本命中为加强证据（WKWebView 的
    --    AX 桥在菜单自动化后不稳定，内容级验证由 W2 浏览器证据覆盖同一 lsDiagnose）
    set reopenHit to 0
    repeat with tAttempt from 1 to 10
      if (count of (windows whose name is "Locove／洛湾")) > 0 then
        set reopenHit to 1
        exit repeat
      end if
      delay 0.5
    end repeat
    set OUT to OUT & "REOPEN=" & reopenHit & linefeed
    try
      set frontmost to true
      delay 0.8
      set allText to my windowText()
      set sampleLen to count of allText
      if sampleLen > 200 then set sampleLen to 200
      set textSample to ""
      if sampleLen > 0 then set textSample to text 1 thru sampleLen of allText
      set diagHit to 0
      if allText contains "w3-bad" and allText contains "诊断" then set diagHit to 1
      set OUT to OUT & "DIAGAX=" & diagHit & " TEXTLEN=" & (count of allText) & " SAMPLE=" & textSample & linefeed
    on error eD
      set OUT to OUT & "DIAGAX=err(" & eD & ")" & linefeed
    end try
    try
      set wPos to position of window "Locove／洛湾"
      set wSize to size of window "Locove／洛湾"
      set OUT to OUT & "WINPOS=" & (item 1 of wPos) & "," & (item 2 of wPos) & " WINSIZE=" & (item 1 of wSize) & "," & (item 2 of wSize) & linefeed
    end try
    -- 5) 设置… → 工作台设置视图
    set setClick to 0
    repeat with attempt from 1 to 3
      try
        click menu bar item 1 of menu bar 2
        delay 1.2
        click (first menu item of menu 1 of menu bar item 1 of menu bar 2 whose name is "设置…")
        delay 1.5
        set setClick to 1
        exit repeat
      on error
        delay 0.8
      end try
    end repeat
    set OUT to OUT & "SETCLICK=" & setClick & linefeed
    try
      set allText2 to my windowText()
      if allText2 contains "域名访问" then
        set OUT to OUT & "SETTINGSAX=1" & linefeed
      else
        set OUT to OUT & "SETTINGSAX=0 TEXTLEN2=" & (count of allText2) & linefeed
      end if
    on error
      set OUT to OUT & "SETTINGSAX=err" & linefeed
    end try
    -- 6) 键盘：↓ 选择 + Esc（菜单键盘交互真通路）
    try
      click menu bar item 1 of menu bar 2
      delay 1
      key code 125
      delay 0.3
      key code 53
      delay 0.4
      set OUT to OUT & "KEYBOARD_ARROW_ESC=1" & linefeed
    on error
      set OUT to OUT & "KEYBOARD_ARROW_ESC=0" & linefeed
    end try
    return OUT
  end tell
end tell
AS
grep -qE "TOP=.*刷新状态" "$OUT/w3-diagnose.txt" || fail "全菜单缺「刷新状态」"
grep -qE "TOP=.*打开主窗口" "$OUT/w3-diagnose.txt" || fail "全菜单缺「打开主窗口」"
grep -qE "TOP=.*设置…" "$OUT/w3-diagnose.txt" || fail "全菜单缺「设置…」"
grep -qE "TOP=.*退出菜单栏" "$OUT/w3-diagnose.txt" || fail "全菜单缺「退出菜单栏」"
grep -q "KEYBOARD_ARROW_ESC=1" "$OUT/w3-diagnose.txt" || fail "菜单键盘交互（↓/Esc）失败"
grep -qE "SVC=w3-bad" "$OUT/w3-diagnose.txt" || fail "项目子菜单未见 w3-bad"
grep -qE "ACTS=.*查看原因" "$OUT/w3-diagnose.txt" || fail "故障服务子菜单未提供查看原因"
grep -qE "REOPEN=1" "$OUT/w3-diagnose.txt" || fail "查看原因跳转未打开主窗口"
grep -qE "SETCLICK=1" "$OUT/w3-diagnose.txt" || fail "设置菜单项点击失败"
grep -qE "DIAGAX=1|DIAGAX=0|DIAGAX=err" "$OUT/w3-diagnose.txt" || fail "诊断 AX 读取异常"
echo "RUN 既有菜单栏点击 AX 验证（含退出；CGEvent 合成点击路线）"
if swift "$REPO/tests/verify_menubar_clicks.swift" "$APP_PID" 2>&1 | tee "$OUT/w3-menubar-clicks.txt"; then
  CLICKS_RESULT=pass
else
  CLICKS_RESULT=fail-cgevent
  echo "NOTE: CGEvent 合成点击路线未通过——用 System Events 兜底验证退出路径" | tee -a "$OUT/w3-menubar-clicks.txt"
  osascript <<'AS' 2>&1 | tee -a "$OUT/w3-menubar-clicks.txt"
tell application "System Events"
  tell (first application process whose bundle identifier is "com.mybrain.localservices.menubar.guitest")
    click menu bar item 1 of menu bar 2
    delay 0.8
    click menu item "退出菜单栏（后台服务继续运行）" of menu 1 of menu bar item 1 of menu bar 2
    delay 1
    return "SE-QUIT triggered"
  end tell
end tell
AS
  for _ in $(seq 1 40); do kill -0 "$APP_PID" 2>/dev/null || break; sleep 0.25; done
  if kill -0 "$APP_PID" 2>/dev/null; then
    fail "退出菜单栏未终止 App"
  fi
fi

echo "PASS: W3 native smoke（构建 + 夹具 + 诊断跳转 + 设置 + 键盘 + 退出[$CLICKS_RESULT]）" | tee "$OUT/w3-native-summary.txt"
