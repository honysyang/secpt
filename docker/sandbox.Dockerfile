# secpt 执行沙箱镜像：agent 的 bash 工具在此环境内运行（宿主隔离）
# 构建: make sandbox-image
# 内置渗透常用 CLI（与引擎 bash 白名单对齐）
FROM debian:bookworm-slim

# 默认 deb.debian.org 源即可（构建环境实测通畅）；如需内网镜像在此 sed 切换
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates openssl \
        nmap whatweb \
        python3 \
        grep sed mawk findutils coreutils file \
        unzip \
        dnsutils whois \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
# 非 root 运行 + /work 开放写（引擎以宿主 uid 注入容器，bind mount 的文件归属才能对上）
RUN useradd -m secpt && chmod 777 /work
USER secpt

CMD ["sleep", "infinity"]
