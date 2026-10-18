# secpt pwn 专用执行沙箱：基于通用沙箱叠加二进制利用工具链
# 构建: make sandbox-pwn-image（先构建基础镜像 make sandbox-image）
# 覆盖 pwn/crypto/QEMU 逃逸场景：pwntools 脚本化交互、ROP/格式化、内核题
FROM secpt-sandbox

RUN apt-get update && apt-get install -y --no-install-recommends \
        patchelf \
    && pip3 install --break-system-packages --no-cache-dir \
        pwntools ROPgadget z3-solver \
    && rm -rf /var/lib/apt/lists/*
# checksec/cyclic 由 pwntools 提供；z3 做约束求解（f2-05 式"手搓解密器"的正规化）；
# qemu 用题目自带二进制（不进镜像）

WORKDIR /work
USER secpt
CMD ["sleep", "infinity"]
