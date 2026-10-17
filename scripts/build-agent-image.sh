#!/bin/bash
# 构建并打包比赛镜像（Kali 基座，全部工具离线预装）
#
# 用法:
#   bash scripts/build-agent-image.sh                 # 构建 + 导出 agent.tar.gz
#   KEEP_BUILDER=0 bash scripts/build-agent-image.sh  # 完成后删除 buildx 构建器缓存
#   LOAD=1 bash scripts/build-agent-image.sh          # 额外 docker load 到本地（4.6GB，较慢）
#
# 说明:
# - 不使用 `docker save`：本机 Docker 的 save 路径可能死锁（连小镜像也 0 字节）。
#   这里用独立 buildx 构建器 + `--output type=docker,dest=...` 直接导出 tar。
# - 构建期需要网络：Kali apt 走清华镜像、PyPI 走清华镜像、Docker Hub 走 registry mirrors。
# - 运行期完全离线：工具/浏览器/知识种子都在镜像里，不做任何 apt/pip/浏览器下载。
set -euo pipefail
cd "$(dirname "$0")/.."

IMAGE="${IMAGE:-secpt-agent:latest}"
OUT="${OUT:-agent.tar.gz}"
BUILDER="${SECPT_BUILDER:-secpt-export}"
MIRROR_CFG="${SECPT_BUILDER_CFG:-/tmp/secpt-buildkitd.toml}"
TAR="/tmp/secpt-agent-export.tar"
LIMIT_GB=3

echo "[build] 1/6 生成脱敏知识种子（flag/token/私钥一律打码）"
python3 scripts/prepare-image-seed.py

echo "[build] 2/6 准备独立 buildx 构建器（带 Docker Hub 镜像加速）"
if ! docker buildx inspect "$BUILDER" >/dev/null 2>&1; then
    cat > "$MIRROR_CFG" <<'EOF'
[registry."docker.io"]
  mirrors = ["docker.m.daocloud.io", "docker.mirrors.ustc.edu.cn", "docker.1ms.run", "hub.rat.dev"]
EOF
    docker buildx create --name "$BUILDER" --driver docker-container --config "$MIRROR_CFG" --bootstrap
else
    echo "[build] 复用已有构建器: $BUILDER"
fi

echo "[build] 3/6 构建并直接导出 docker 格式 tar（绕开 docker save）"
rm -f "$TAR"
docker buildx build \
    --builder "$BUILDER" \
    --progress=plain \
    --output "type=docker,dest=$TAR" \
    -f docker/agent.Dockerfile \
    -t "$IMAGE" \
    .

echo "[build] 4/6 校验 tar：镜像名 / 无凭证环境变量 / 层数"
python3 - "$TAR" <<'PY'
import json, sys, tarfile
path = sys.argv[1]
with tarfile.open(path) as tf:
    man = json.load(tf.extractfile("manifest.json"))
    assert man and man[0].get("RepoTags") == ["secpt-agent:latest"], man[0].get("RepoTags")
    cfg = json.load(tf.extractfile(man[0]["Config"]))
    env = "\n".join(cfg.get("config", {}).get("Env") or [])
    assert "DEEPSEEK_API_KEY" not in env, "镜像环境变量里不应有 LLM key"
    assert "BENCHMARK_TOKEN" not in env, "镜像环境变量里不应有平台 token"
    assert "Entrypoint" in cfg.get("config", {}) or cfg.get("config", {}).get("Entrypoint")
    print(f"[check] image=secpt-agent:latest layers={len(man[0]['Layers'])} env_ok=True")
PY

echo "[build] 5/6 压缩为 $OUT（先写临时文件，失败不覆盖旧包）"
rm -f "$OUT.tmp"
gzip -c "$TAR" > "$OUT.tmp"
mv "$OUT.tmp" "$OUT"
gzip -t "$OUT"
SIZE_BYTES=$(stat -c%s "$OUT")
SIZE_H=$(du -h "$OUT" | cut -f1)
SIZE_GB=$(awk -v b="$SIZE_BYTES" 'BEGIN{printf "%.2f", b/1024/1024/1024}')
echo "[build] 6/6 完成: $OUT ($SIZE_H, ${SIZE_GB}GB / 上限 ${LIMIT_GB}GB)"
sha256sum "$OUT"
if awk -v g="$SIZE_GB" -v lim="$LIMIT_GB" 'BEGIN{exit !(g>lim)}'; then
    echo "[build] 错误：超过 ${LIMIT_GB}GB 上限（裁剪：qemu-user / hashcat / chromium / 字典）"
    exit 1
fi
rm -f "$TAR"

if [ "${LOAD:-0}" = "1" ]; then
    echo "[build] docker load（4.6GB，可能较慢）..."
    docker load -i "$OUT"
    echo "[build] 本地冒烟：无凭证应快速退出"
    docker run --rm "$IMAGE" || true
fi
if [ "${KEEP_BUILDER:-1}" = "0" ]; then
    echo "[build] 删除 buildx 构建器缓存: $BUILDER"
    docker buildx rm "$BUILDER" >/dev/null 2>&1 || true
fi

echo "[build] 上传件: $(pwd)/$OUT"
echo "[build] 平台运行时环境变量至少设置: DEEPSEEK_API_KEY（BENCHMARK_* 平台自动注入）"
