# secpt 执行沙箱镜像：agent 的 bash 工具在此环境内运行（宿主隔离）
# 构建: make sandbox-image
# 内置渗透常用 CLI（与引擎 bash 白名单对齐）
FROM debian:bookworm-slim

# 默认 deb.debian.org 源即可（构建环境实测通畅）；如需内网镜像在此 sed 切换
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates openssl \
        nmap whatweb dnsutils whois \
        python3 python3-pip sqlite3 \
        grep sed mawk findutils coreutils file \
        unzip \
        # 暴力破解 / 注入自动化
        hydra john hashcat sqlmap wfuzz ffuf \
        # 隐写 / 取证 / 逆向
        steghide binwalk exiftool gdb radare2 \
        # 网络链路 / 流量
        netcat-openbsd tcpdump \
    && rm -rf /var/lib/apt/lists/*

# 浏览器插件（tools/plugins/browser.py）：playwright + 自带 chromium（--with-deps
# 同时装齐系统库）。插件在 engine 进程内执行：镜像内开箱即用；非 root 下 chromium
# 必须 no-sandbox（插件读 SECPT_BROWSER_NO_SANDBOX，由 ENV 默认声明）
RUN pip3 install --break-system-packages --no-cache-dir playwright \
    && python3 -m playwright install --with-deps chromium \
    && rm -rf /var/lib/apt/lists/*
ENV SECPT_BROWSER_NO_SANDBOX=1

WORKDIR /work
# 非 root 运行 + /work 开放写（引擎以宿主 uid 注入容器，bind mount 的文件归属才能对上）
RUN useradd -m secpt && chmod 777 /work
USER secpt

CMD ["sleep", "infinity"]
