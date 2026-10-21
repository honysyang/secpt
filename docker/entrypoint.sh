#!/bin/bash
# secpt 比赛镜像入口：凭证检查 → 知识种子 → 平台探测（仅记录）→ 自动解题
# 说明：比赛环境不依赖 VPN，不做 openvpn 建链、不因 VPN 探针失败退出。
set -u
cd /app
mkdir -p /app/var/run /app/var/knowledge/playbooks

echo "[entry] secpt benchmark agent 启动 $(date -Is)"

# ── 0. 运行时默认值（运行时注入的同名变量一律优先——禁止无条件覆盖）──────
export SECPT_VAR="${SECPT_VAR:-/app/var}"
export SECPT_SANDBOX="${SECPT_SANDBOX:-0}"
# VPN：默认 off（总开关 SECPT_VPN=1 开启，solve 自动 --vpn；连接参数 TYPE/CONFIG/PROBE）
export SECPT_VPN="${SECPT_VPN:-0}"
export SECPT_VPN_TYPE="${SECPT_VPN_TYPE:-none}"
export SECPT_VPN_PROBE_URL="${SECPT_VPN_PROBE_URL:-off}"
export SECPT_TECHNIQUES="${SECPT_TECHNIQUES:-1}"
export SECPT_BENCH_BROWSER="${SECPT_BENCH_BROWSER:-0}"
export SECPT_BENCHMARK_WORKERS="${SECPT_BENCHMARK_WORKERS:-3}"
export SECPT_CAMPAIGN_ROUNDS="${SECPT_CAMPAIGN_ROUNDS:-0}"
export SECPT_BENCH_STEP_WALL_S="${SECPT_BENCH_STEP_WALL_S:-15}"
# R2：RSS 软着陆（0=关）。16G 比赛容器默认 11G：触及即排干本轮→退出码3→重启续跑
export SECPT_RSS_SOFT_MB="${SECPT_RSS_SOFT_MB:-11264}"
SECPT_CHALLENGE_DIR="${SECPT_CHALLENGE_DIR:-/app/challenges/tsec-benchmark}"
SECPT_ENTRY_RETRIES="${SECPT_ENTRY_RETRIES:-999}"
SECPT_ENTRY_RETRY_SLEEP="${SECPT_ENTRY_RETRY_SLEEP:-30}"
# 非法值兜底：seq/sleep 拿到非数字会直接报错，重试链就断了
case "$SECPT_ENTRY_RETRIES" in ''|*[!0-9]*) SECPT_ENTRY_RETRIES=999 ;; esac
case "$SECPT_ENTRY_RETRY_SLEEP" in ''|*[!0-9]*) SECPT_ENTRY_RETRY_SLEEP=30 ;; esac
# 透传 solve 的附加参数（默认空；set -u 下必须先定义再引用）
SECPT_SOLVE_ARGS="${SECPT_SOLVE_ARGS:-}"
_VPN_ARGS=""
[ "$SECPT_VPN" = "1" ] && _VPN_ARGS="--vpn"
: "${BENCHMARK_BASE_URL:=https://tsecbench.zc.tencent.com}"
export BENCHMARK_BASE_URL

# ── 1. 凭证检查（BENCHMARK_* 平台注入；LLM 端点/模型/key 均由运行时环境变量配置）──
if [ -z "${BENCHMARK_TOKEN:-}" ]; then
    echo "[entry] 缺少 BENCHMARK_TOKEN（平台应自动注入）"; exit 1
fi

# LLM 凭证：支持默认 provider key、通用自定义端点 key、任意 provider 前缀 key
llm_key_ok=0
for _v in DEEPSEEK_API_KEY OPENAI_API_KEY ZHIPU_API_KEY SECPT_MODEL_API_KEY; do
    [ -n "${!_v:-}" ] && llm_key_ok=1
done
env | grep -q '^SECPT_MODEL_KEY_' && llm_key_ok=1
if [ "$llm_key_ok" -eq 0 ]; then
    echo "[entry] 缺少 LLM API key：请设置 DEEPSEEK_API_KEY / OPENAI_API_KEY /" \
         "ZHIPU_API_KEY / SECPT_MODEL_API_KEY，或 SECPT_MODEL_KEY_<PREFIX>"
    echo "[entry] 自定义端点示例：SECPT_MODEL_BASE_URL=... SECPT_MODEL_NAME=... SECPT_MODEL_API_KEY=..."
    exit 1
fi

# 第二脑（GLM）默认值：只在确有 GLM 凭证时启用。无 key 时若仍换脑，第 2+ 轮
# 每个意图都会在 resolve_model 处报"缺少 API key"，整轮空烧——比不换更糟。
_glm_key=0
[ -n "${ZHIPU_API_KEY:-}" ] && _glm_key=1
[ -n "${SECPT_MODEL_KEY_GLM:-}" ] && _glm_key=1
if [ "$_glm_key" -eq 1 ]; then
    export SECPT_ESCALATE_MODEL="${SECPT_ESCALATE_MODEL:-glm/glm-5.3-flash}"
    export SECPT_MODEL_POOL="${SECPT_MODEL_POOL:-deepseek-v4-flash,glm/glm-5.3-flash}"
    echo "[entry] GLM 第二脑启用：escalate=$SECPT_ESCALATE_MODEL" \
         "glm_base=${SECPT_MODEL_BASE_GLM:-provider-default}"
    if [ -z "${SECPT_MODEL_BASE_GLM:-}" ]; then
        echo "[entry] 提示：未设 SECPT_MODEL_BASE_GLM，将用 GLM 官方默认端点；" \
             "Coding Plan key 需设 https://open.bigmodel.cn/api/coding/paas/v4"
    fi
else
    echo "[entry] 未检测到 GLM 凭证（ZHIPU_API_KEY / SECPT_MODEL_KEY_GLM）：轮间不换脑"
    if [ -n "${SECPT_ESCALATE_MODEL:-}" ]; then
        echo "[entry] 警告：SECPT_ESCALATE_MODEL 已设置但无 GLM 凭证，" \
             "第 2+ 轮换脑会因缺少 API key 整轮失败——请补 ZHIPU_API_KEY 或清空该变量"
    fi
fi

echo "[entry] base=$BENCHMARK_BASE_URL" \
     "model=${SECPT_MODEL_NAME:-${SECPT_MODEL_PRIMARY:-deepseek-v4-flash}}" \
     "llm_base=${SECPT_MODEL_BASE_URL:-provider-default}" \
     "escalate=${SECPT_ESCALATE_MODEL:-无}" \
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

# ── 4. 自动解题（异常重试；exit 2=平台任务结束/鉴权失败不重试；exit 3=RSS 软着陆，
#      属计划内回收，重试即续跑）。重试默认 999×30s≈8.3h：比赛要求跑满 8 小时，
#      瞬时断网/单轮崩溃都不能让容器提前收工；平台真正结束由 rc=2 精确识别。
#      正常完成（全部题目已通关）solve 返回 0，同样不再重启。──────────────────
echo "[entry] 进入 TSec Benchmark 跑分流程 $(date -Is)"
rc=1
child=""
stop_requested=0
_retry_sleep="$SECPT_ENTRY_RETRY_SLEEP"

# 终止信号统一处理：转发给 solve 并置停止标志。solve 退出码 >128 时直接 break，
# 不会重启——SIGTERM/SIGINT/SIGHUP 不会造成重复重启循环（包括重试 sleep 期间）。
_on_term() {
    echo "[entry] 收到终止信号，转发给 solve 并停止重启"
    stop_requested=1
    if [ -n "${child:-}" ]; then
        kill -TERM "$child" 2>/dev/null || true
    fi
}
trap _on_term TERM INT HUP

for attempt in $(seq 1 "$SECPT_ENTRY_RETRIES"); do
    if [ "$stop_requested" -eq 1 ]; then
        break
    fi
    # 清掉上一段 solve 遗留的孤儿工具进程（后台 http.server/nc/chromium 等）：
    # 它们跨重启存活会占住内存，让新进程很快再次触及软限（重启死循环）。
    # 此时代码路径上 solve 已退出、重试 sleep 未开始，PID 1 的直接子进程只可能是孤儿。
    pkill -9 -P 1 2>/dev/null || true
    _attempt_t0="$(date +%s)"
    # SECPT_SOLVE_ARGS：透传附加参数（如 --no-watch；默认空；--vpn 由 SECPT_VPN 控制）
    # shellcheck disable=SC2086
    python -u -m secpt.cli.app solve "$SECPT_CHALLENGE_DIR" --wall 0 $_VPN_ARGS $SECPT_SOLVE_ARGS &
    child=$!
    # 硬 OOM 兜底：内核按 oom_score 选牺牲者。提高 solve 的分数（正数无需特权，
    # 失败静默）让它先于 PID 1 被选中——entrypoint 活着才能重启续跑。
    echo 500 > "/proc/$child/oom_score_adj" 2>/dev/null || true
    wait "$child"
    rc=$?
    if [ "$stop_requested" -eq 1 ]; then
        # 主动停止（平台销毁/终端关闭）：等 solve 收尾后退出，不重启
        wait "$child" 2>/dev/null || true
        child=""
        echo "[entry] 收到停止信号（rc=$rc），容器退出"
        break
    fi
    # 仅 SIGINT/SIGHUP/SIGTERM 视为主动停止；137(SIGKILL/OOM)、139(SIGSEGV)、
    # 134(SIGABRT) 是崩溃/被硬杀，必须走下面的重试续跑——这正是软着陆/重启链的价值。
    case "$rc" in
        129|130|143)
            wait "$child" 2>/dev/null || true
            child=""
            echo "[entry] solve 被信号终止（rc=$rc），容器退出"
            break
            ;;
    esac
    child=""
    if [ "$rc" -eq 0 ] || [ "$rc" -eq 2 ]; then
        break
    fi
    # 崩溃循环退避：短命失败（<120s）翻倍等待，上限 300s，避免快速刷屏空转
    if [ $(( $(date +%s) - _attempt_t0 )) -lt 120 ]; then
        _retry_sleep=$(( _retry_sleep * 2 ))
        if [ "$_retry_sleep" -gt 300 ]; then _retry_sleep=300; fi
    else
        _retry_sleep="$SECPT_ENTRY_RETRY_SLEEP"
    fi
    echo "[entry] solve 异常退出（rc=$rc），第 $attempt/$SECPT_ENTRY_RETRIES 次重试前等待 ${_retry_sleep}s..."
    sleep "$_retry_sleep" &
    wait $! 2>/dev/null || true
done
trap - TERM INT HUP
echo "[entry] 进程结束 rc=$rc $(date -Is)"
exit "$rc"
