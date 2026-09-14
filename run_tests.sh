#!/bin/bash
# run_tests.sh —— xauusd-analyze-v3 唯一測試入口（2026-09-14）
#
# 為何要呢個：2026-09-14 統一 test_paper_trade_guards.py 之後，test file 分佈喺
# repo root 同 scripts/ 兩邊（root 13 個 + scripts/ 7 個）。慣性跑 root glob
# `python3 test_*.py` 會靜靜咁漏晒 scripts/ 嗰批（stacking / seed / path overlay
# 斷言），所以跑測試一律用呢個 script，唔好再自己 glob。
#
# 語義：
#   • pass/fail 以 **exit code** 為準（唔靠 grep 文字；唔少 test summary 寫「0 failed」）
#   • 額外印 output 內 FAIL / ERROR / Traceback 開頭嘅行做線索（唔影響判定）
#   • 預設注入 sandbox env（XAUUSD_PAPER_LOG / XAUUSD_PAPER_MARTINGALE /
#     XAUUSD_PUSH_HISTORY → temp），防止 fixture 寫到真 ledger／真 dedupe log
#   • run 前後對 3 個 live state 檔 sha256：
#       – 冇變            → ✅
#       – 有變 + cron tick 冇同時跑 → 🔴 fail（= 有 test 寫到真檔，即 09-14 污染同類事故）
#       – 有變 + cron tick 同時跑   → ⚠️ 唔 fail，只提示（tick 本身會寫 float/status，無法歸因）
#   • exit 0 只限「全部 test exit 0」＋ live state 冇未歸因改動
#
# 用法：
#   bash run_tests.sh                          # 掃 root + scripts/ 全部
#   bash run_tests.sh test_limit_fill.py       # 只跑指定檔（照樣驗 live state）
#   PYTHON=/opt/homebrew/bin/python3 bash run_tests.sh
#   RUN_TESTS_NO_SANDBOX=1 bash run_tests.sh   # 唔注入 sandbox env（debug 用）
#   RUN_TESTS_STATE_DIR=/tmp/x bash run_tests.sh   # 改 live state 目錄（self-test 用）
set -u

cd "$(dirname "$0")" || exit 1

PY="${PYTHON:-/usr/bin/python3}"
STATE_DIR="${RUN_TESTS_STATE_DIR:-$HOME/.hermes/reports}"
# XAUUSD cron job 嘅 output 目錄 —— 用嚟判斷 run 期間有冇 cron tick 同時寫 live state
CRON_OUT="${RUN_TESTS_CRON_OUT:-$HOME/.hermes/cron/output/904a8f1758b6}"
# self-test hooks（正常唔需要設）：俾 child test 見到呢兩個 override
[ -n "${RUN_TESTS_STATE_DIR:-}" ] && export RUN_TESTS_STATE_DIR
[ -n "${RUN_TESTS_CRON_OUT:-}" ] && export RUN_TESTS_CRON_OUT
STATE_FILES=(push_history.json paper_trade_log.json paper_martingale.json)

RUN_START="$(date +%s)"

snapshot_state() {
  for f in "${STATE_FILES[@]}"; do
    if [ -f "${STATE_DIR}/${f}" ]; then
      printf '%s  %s\n' "$(shasum -a 256 "${STATE_DIR}/${f}" | cut -d' ' -f1)" "$f"
    fi
  done
}

# ── sandbox env ──
SANDBOX_DIR=""
if [ "${RUN_TESTS_NO_SANDBOX:-0}" != "1" ]; then
  SANDBOX_DIR="$(mktemp -d "${TMPDIR:-/tmp}/xauusd_test_sandbox.XXXXXX")"
  export XAUUSD_PAPER_LOG="${SANDBOX_DIR}/paper_trade_log.json"
  export XAUUSD_PAPER_MARTINGALE="${SANDBOX_DIR}/paper_martingale.json"
  export XAUUSD_PUSH_HISTORY="${SANDBOX_DIR}/push_history.json"
fi

# ── 收集 test files ──
if [ "$#" -gt 0 ]; then
  FILES=("$@")
else
  FILES=()
  for f in test_*.py scripts/test_*.py; do [ -f "$f" ] && FILES+=("$f"); done
fi

if [ "${#FILES[@]}" -eq 0 ]; then
  echo "❌ 搵唔到任何 test 檔（要喺 repo root 跑）"
  exit 1
fi

echo "🧪 xauusd-analyze-v3 test run — $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo "   python     : ${PY}"
echo "   sandbox    : ${SANDBOX_DIR:-關（RUN_TESTS_NO_SANDBOX=1）}"
echo "   live state : ${STATE_DIR}"
echo

BEFORE="$(snapshot_state)"

pass=0; fail=0; FAILED=()
for f in "${FILES[@]}"; do
  if [ ! -f "$f" ]; then
    printf '  ❌ %-46s (冇呢個檔)\n' "$f"
    fail=$((fail+1)); FAILED+=("$f")
    continue
  fi
  out="$("$PY" "$f" 2>&1)"; rc=$?
  if [ "$rc" -eq 0 ]; then
    printf '  ✅ %-46s\n' "$f"
    pass=$((pass+1))
  else
    printf '  ❌ %-46s exit=%s\n' "$f" "$rc"
    fail=$((fail+1)); FAILED+=("$f")
    printf '%s\n' "$out" | grep -E '^[[:space:]]*(FAIL|ERROR|Traceback|AssertionError)' | head -4 | sed 's/^/        /'
  fi
done

echo
AFTER="$(snapshot_state)"

changed=""
for f in "${STATE_FILES[@]}"; do
  b="$(printf '%s\n' "$BEFORE" | grep " ${f}\$" || true)"
  a="$(printf '%s\n' "$AFTER" | grep " ${f}\$" || true)"
  [ "${b}" = "${a}" ] || changed="${changed:+${changed} }${f}"
done

# cron tick 有冇喺 run 期間寫過（output .md 新過 run 開始，或者 paper_trade.py 仲跑緊）
tick_seen=0
newest_md="$(/bin/ls -t "${CRON_OUT}" 2>/dev/null | head -1 || true)"
if [ -n "${newest_md}" ] && [ -f "${CRON_OUT}/${newest_md}" ]; then
  md_m="$(stat -f %m "${CRON_OUT}/${newest_md}" 2>/dev/null || echo 0)"
  [ "${md_m}" -ge "${RUN_START}" ] && tick_seen=1
fi
pgrep -f "paper_trade.py" >/dev/null 2>&1 && tick_seen=1

echo "────────────────────────────────────────────────────────"
printf '  total=%s  pass=%s  fail=%s\n' "${#FILES[@]}" "$pass" "$fail"
[ -n "${SANDBOX_DIR}" ] && rm -rf "${SANDBOX_DIR}"

rc=0
if [ -z "${changed}" ]; then
  echo "  ✅ live state 前後一致 (${STATE_DIR})"
elif [ "${tick_seen}" -eq 1 ]; then
  echo "  ⚠️  live state 有變（${changed}）— 但同期有 cron tick / paper_trade 寫入，唔算 fail"
  echo "      要硬證冇污染：等兩個 tick 之間（cron :01/:11/:21/:31/:41 每 10 分鐘）再跑一次"
else
  echo "  🔴 live state 冇 cron tick 都變咗（${changed}）— 有 test 寫到真檔，即刻查"
  rc=1
fi

if [ "$fail" -gt 0 ]; then
  echo "  ❌ FAILED:"
  for f in "${FAILED[@]}"; do echo "       ${f}"; done
  rc=1
elif [ "${rc}" -eq 0 ]; then
  echo "  🎉 全部 pass"
fi

exit "${rc}"
