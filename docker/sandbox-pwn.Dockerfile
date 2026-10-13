# secpt 沙箱·二进制/crypto 扩展镜像（pwntools 官方底 + 密码学 Python 库）
# 构建: make sandbox-pwn-image；profile 的 sandbox_image 字段自动选用
FROM pwntools/pwntools:latest

USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        checksec qemu-user \
    && pip install --no-cache-dir pycryptodome sympy gmpy2 z3-solver \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /work
RUN chmod 777 /work
# pwntools 镜像自带 pwntools 用户；以宿主 uid 注入运行（同 web 沙箱语义）
CMD ["sleep", "infinity"]
