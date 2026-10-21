# secpt 比赛自包含镜像（Kali 基座：工具覆盖优先，全部离线预装）
#
# 比赛姿态：单镜像方案——智能体与工具执行同容器直跑（SECPT_SANDBOX=0，
# bash 直接用镜像内的 Kali 工具，无第二容器/无 docker-in-docker）。
# 仓库里的 sandbox*.Dockerfile 是宿主机本地可选的执行沙箱，比赛镜像不使用。
#
# 平台契约：
# - 启动后自动跑 benchmark（ENTRYPOINT 无参）
# - BENCHMARK_TOKEN / BENCHMARK_BASE_URL 由平台运行时注入
# - DEEPSEEK_API_KEY 需在平台「运行时环境变量」手动添加（绝不打进镜像）
# - 需要第 2+ 轮换脑/故障转移时再注入 ZHIPU_API_KEY（GLM Coding Plan 还需
#   SECPT_MODEL_BASE_GLM=https://open.bigmodel.cn/api/coding/paas/v4）；
#   entrypoint 检测到 GLM 凭证才启用第二脑，无 key 不会把第 2 轮跑废
# - 内网场景：运行时注入 SECPT_VPN_TYPE=openvpn + SECPT_VPN_CONFIG（容器内路径）
#   + SECPT_VPN_PROBE_URL，并 --cap-add NET_ADMIN --device /dev/net/tun；
#   镜像已含 openvpn 二进制，但绝不内置任何 VPN 配置/凭证
#
# 为什么用 Kali：Debian 源缺 radare2/nikto/nuclei/feroxbuster/checksec/subfinder
# 等安全工具；Kali 源全部可 apt 离线预装，避免运行时拉取。Python 用 3.13 venv
# （Kali 默认 python3 是 3.14，部分依赖尚未适配）。
#
# 构建:  scripts/build-agent-image.sh
# 本地冒烟: docker run --rm -e DEEPSEEK_API_KEY=... -e BENCHMARK_TOKEN=... secpt-agent:latest

FROM kalilinux/kali-rolling

ENV DEBIAN_FRONTEND=noninteractive \
    PIP_INDEX_URL=https://pypi.tuna.tsinghua.edu.cn/simple

# Kali 官方源在部分网络下只有 ~30KB/s；切清华镜像（本机实测 30MB/s）。
# 用 HTTP：Kali 基础镜像未带 ca-certificates，HTTPS 镜像会证书校验失败；
# Release 文件有 GPG 签名，HTTP 传输仍可校验包完整性。
RUN sed -i 's|http://http.kali.org/kali/|http://mirrors.tuna.tsinghua.edu.cn/kali/|' /etc/apt/sources.list.d/kali.sources \
    && sed -i 's|https://mirrors.tuna.tsinghua.edu.cn/kali/|http://mirrors.tuna.tsinghua.edu.cn/kali/|' /etc/apt/sources.list.d/kali.sources 2>/dev/null || true

# 工具层（显式清单，不用 kali-linux-headless 元包——1835 个包、含 GUI/冗余，体积失控）。
# 刻意不装 seclists 全量（安装后 ~1.85GB）；用 dirb/wfuzz 自带字典 + 构建时精选。
# 实战缺口补全（1202/1323 复盘）：telnet（c-07 弱口令实战）/ssh 客户端（session 驱动）/
# redis·mysql·pgsql 客户端（拿到凭据后的连接面）/smbclient+snmp（内网族）/
# zip+7z（附件归档）/ping。
RUN apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 update \
    && apt-get install -y --no-install-recommends \
        python3.13 python3.13-venv python3.13-dev \
        curl wget ca-certificates openssl iproute2 procps iputils-ping \
        openvpn openssh-client telnet \
        nmap whatweb bind9-dnsutils bind9-host whois masscan socat \
        sqlite3 grep sed gawk findutils coreutils file unzip zip p7zip-full binutils \
        hydra john hashcat sqlmap wfuzz ffuf gobuster dirb feroxbuster \
        nikto nuclei subfinder arjun \
        redis-tools default-mysql-client postgresql-client smbclient snmp \
        radare2 gdb strace ltrace patchelf upx-ucl checksec \
        binwalk foremost sleuthkit pngcheck steghide libimage-exiftool-perl \
        netcat-openbsd tcpdump jq git perl php-cli nodejs npm qemu-user \
        default-jre-headless chromium \
    && rm -rf /var/lib/apt/lists/*

# 精选字典（不装 1.85GB 的 seclists 包；下载失败不阻断构建）。
# 关键：建立 /usr/share/wordlists/seclists/... 兼容软链——agent 习惯引用该路径
# （1202 实证：引用了镜像里不存在的 seclists 路径，fuzz 直接落空烧步）。
RUN mkdir -p /usr/share/wordlists/custom /usr/share/wordlists/seclists/Discovery/Web-Content \
    && for u in \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/raft-small-words.txt" \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/raft-small-directories-lowercase.txt" \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/raft-medium-directories.txt" \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/raft-medium-files-small.txt" \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/directory-list-2.3-small.txt" \
        "https://cdn.jsdelivr.net/gh/danielmiessler/SecLists@master/Discovery/Web-Content/quickhits.txt"; do \
        curl -fsSL --max-time 30 "$u" -o "/usr/share/wordlists/custom/$(basename "$u")" || true; \
    done \
    && ln -sf /usr/share/dirb/wordlists/common.txt /usr/share/wordlists/custom/common.txt \
    && for f in /usr/share/wordlists/custom/*; do \
        ln -sf "$f" "/usr/share/wordlists/seclists/Discovery/Web-Content/$(basename "$f")"; \
    done \
    && ls -la /usr/share/wordlists/custom | head

WORKDIR /app

# 1) secpt 本体 + 插件（wheel force-include 把 tools/plugins 打进包内）
COPY pyproject.toml README.md ./
COPY src ./src
COPY tools ./tools
RUN python3.13 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir .

# 2) Python 武器库（分组可选：任一组失败不阻断主镜像）
RUN /opt/venv/bin/pip install --no-cache-dir pycryptodome cryptography z3-solver sympy || echo "[build] crypto python 工具跳过"
RUN /opt/venv/bin/pip install --no-cache-dir mitmproxy || echo "[build] mitmproxy 跳过"
RUN (/opt/venv/bin/pip install --no-cache-dir pwntools ropper ROPgadget || echo "[build] pwn python 工具跳过") || true

# 3) 浏览器：系统 chromium（Kali 源，离线预装）+ playwright Python 绑定。
#    browser.py 自带"系统 chromium 回退"逻辑，不需要 playwright 从 CDN 下载浏览器
#    （CDN 在部分网络被限速，构建期拉取会拖死）。
RUN /opt/venv/bin/pip install --no-cache-dir playwright || echo "[build] playwright 绑定跳过"

# 4) nuclei 模板（可选；失败则 nuclei 仅能跑自定义模板）
RUN (timeout 120 nuclei -update-templates -silent || true)

# 5) 运行时题面模板（无 token/无 VPN 配置；凭证全部来自平台环境变量）
COPY docker/task-template.md ./challenges/tsec-benchmark/test.md

# 6) 脱敏知识种子（playbooks/experiences；构建前由脚本生成）
COPY seed /opt/secpt-seed
COPY docker/entrypoint.sh ./entrypoint.sh
RUN chmod +x ./entrypoint.sh

ENV PATH=/opt/venv/bin:$PATH \
    SECPT_MODEL_PRIMARY=deepseek-v4-flash \
    SECPT_MODEL_FALLBACK=deepseek-v4-flash \
    SECPT_CONTEXT_WINDOW=128000 \
    SECPT_VPN_TYPE=none \
    SECPT_VPN_PROBE_URL=off \
    SECPT_SANDBOX=0 \
    SECPT_TECHNIQUES=1 \
    SECPT_BENCH_BROWSER=0 \
    SECPT_BENCHMARK_WORKERS=3 \
    SECPT_CAMPAIGN_ROUNDS=0 \
    SECPT_BENCH_STEP_WALL_S=15 \
    SECPT_BROWSER_NO_SANDBOX=1 \
    SECPT_ENABLE_CRYPTO_HELPERS=1 \
    SECPT_VAR=/app/var

ENTRYPOINT ["/app/entrypoint.sh"]
