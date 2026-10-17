#!/bin/bash
# secpt 比赛镜像入口：凭证检查 → 知识种子 → 平台探测（仅记录）→ 自动解题
# 说明：比赛环境不依赖 VPN，不做 openvpn 建链、不因 VPN 探针失败退出。
set -u
cd /app
mkdir -p /app/var/run /app/var/knowledge/playbooks

echo "[entry] secpt benchmark agent 启动 $(date -Is)"

# ── 0. 运行时默认值（平台注入的同名变量优先）────────────────────────
export SECPT_VAR=/app/var
export SECPT_SANDBOX=0
export SECPT_VPN_PROBE_URL=off        # 比赛环境无 VPN；health 跳过 VPN 判定
export SECPT_TECHNIQUES="${SECPT_TECHNIQUES:-1}"
export SECPT_BENCH_BROWSER="${SECPT_BENCH_BROWSER:-0}"
export SECPT_BENCHMARK_WORKERS="${SECPT_BENCHMARK_WORKERS:-3}"
export SECPT_CAMPAIGN_ROUNDS="${SECPT_CAMPAIGN_ROUNDS:-3}"
export SECPT_BENCH_STEP_WALL_S="${SECPT_BENCH_STEP_WALL_S:-10}"
: "${BENCHMARK_BASE_URL:=https://tsecbench.zc.tencent.com}"
export BENCHMARK_BASE_URL

# ── 1. 凭证检查（BENCHMARK_* 平台注入；LLM 端点/模型/key 均由运行时环境变量配置）──
if [ -z "${BENCHMARK_TOKEN:-}" ]; then
    echo "[entry] 缺少 BENCHMARK_TOKEN（平台应自动注入）"; exit 1
fi

# LLM 凭证：支持默认 provider key、通用自定义端点 key、任意 provider 前缀 key
llm_key_ok=0
for _v in DEEPSEEK_API_KEY OPENAI_API_KEY SECPT_MODEL_API_KEY; do
    [ -n "${!_v:-}" ] && llm_key_ok=1
done
env | grep -q '^SECPT_MODEL_KEY_' && llm_key_ok=1
if [ "$llm_key_ok" -eq 0 ]; then
    echo "[entry] 缺少 LLM API key：请设置 DEEPSEEK_API_KEY / OPENAI_API_KEY /" \
         "SECPT_MODEL_API_KEY，或 SECPT_MODEL_KEY_<PREFIX>"
    echo "[entry] 自定义端点示例：SECPT_MODEL_BASE_URL=... SECPT_MODEL_NAME=... SECPT_MODEL_API_KEY=..."
    exit 1
fi
echo "[entry] base=$BENCHMARK_BASE_URL" \
     "model=${SECPT_MODEL_NAME:-${SECPT_MODEL_PRIMARY:-deepseek-v4-flash}}" \
     "llm_base=${SECPT_MODEL_BASE_URL:-provider-default}" \
     "workers=$SECPT_BENCHMARK_WORKERS rounds=$SECPT_CAMPAIGN_ROUNDS"

# ── 2. 知识种子（脱敏后的历史打法；首启复制，不覆盖运行期新产物）────
if [ -d /opt/secpt-seed/knowledge ]; then
    mkdir -p /app/var/knowledge/playbooks
    for f in /opt/secpt-seed/knowledge/playbooks/*.md; do
        [ -e "$f" ] || continue
        b="$(basename "$f")"
        [ -f "/app/var/knowledge/playbooks/$b" ] || cp "$f" "/app/var/knowledge/playbooks/$b"
    done
    [ -f /opt/secpt-seed/knowledge/experiences.jsonl ] && \
        [ ! -f /app/var/knowledge/experiences.jsonl ] && \
        cp /opt/secpt-seed/knowledge/experiences.jsonl /app/var/knowledge/experiences.jsonl
    echo "[entry] 知识种子已就位（playbooks=$(ls /app/var/knowledge/playbooks 2>/dev/null | wc -l)）"
fi

# ── 3. 平台可达性探测（仅记录，不阻塞；真实鉴权由 solve 处理）────────
code="$(curl -s -m 10 -o /dev/null -w "%{http_code}" \
    -H "BENCHMARK_TOKEN: ${BENCHMARK_TOKEN}" \
    "${BENCHMARK_BASE_URL%/}/openapi/v1/challenges" 2>/dev/null)"
echo "[entry] 平台探测 HTTP ${code:-000}（404/409 也会由 solve 明确报告）"

# ── 4. 自动解题（异常重试一次；exit 2=平台任务结束/鉴权失败，不重试）──
echo "[entry] 进入 TSec Benchmark 跑分流程 $(date -Is)"
rc=1
for attempt in 1 2; do
    python -u -m secpt.cli.app solve /app/challenges/tsec-benchmark --wall 0
    rc=$?
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then
        break
    fi
    echo "[entry] solve 异常退出（rc=$rc），第 $attempt 次重试前等待 15s..."
    sleep 15
done
echo "[entry] 进程结束 rc=$rc $(date -Is)"
exit "$rc"
