"""secpt doctor：环境预检（新手第一小时 + CI 前置检查）。

逐项检查运行 secpt 所需的环境，输出 ✓/✗ 与可执行的修复提示。
纯只读：不发起网络请求、不改任何配置（docker 探测除外，超时 10s）。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

import typer

# (显示名, 二进制名, 用途说明)
_TOOLS = [
    ("curl", "curl", "HTTP 请求（几乎所有场景）"),
    ("nmap", "nmap", "端口/服务扫描"),
    ("whatweb", "whatweb", "Web 指纹"),
    ("objdump", "objdump", "二进制分析（pwn/rev）"),
    ("openssl", "openssl", "密码学材料处理"),
    ("node", "node", "sanitize_oracle 插件（mXSS 本地验证）"),
    ("chromium", "chromium", "浏览器内核（browser 插件/sanitize_oracle）"),
]

_OPTIONAL_PY = [
    ("playwright", "browser 插件（pip install 'secpt[browser]' + playwright install chromium）"),
    ("fastembed", "语义检索（pip install 'secpt[embed]'）"),
    ("textual", "TUI 面板（pip install 'secpt[tui]'）"),
    ("mitmproxy", "流量录制（pip install 'secpt[traffic]'）"),
]


def run_checks() -> list[tuple[str, bool, str]]:
    """执行全部检查，返回 [(名称, 是否通过, 详情/修复提示)]。"""
    out: list[tuple[str, bool, str]] = []

    # 1. Python 版本
    ok = sys.version_info >= (3, 12)
    out.append(("python>=3.12", ok, sys.version.split()[0]))

    # 1.5 .env 文件（baseurl / api key 等 .env 导入是否生效的第一可见性）
    from secpt.config.envfile import parse_env_file, project_env_file

    env_path = project_env_file()
    if env_path.is_file():
        n_items = len(parse_env_file(env_path))
        out.append((".env 文件", True, f"{env_path}（{n_items} 个配置项）"))
    else:
        out.append(
            (".env 文件", False, f"{env_path} 不存在——cp .env.example .env 填入 API key 即可")
        )

    # 2. 模型 key（含 provider 路由）
    try:
        from secpt.cli.engine_runner import resolve_models
        from secpt.runtime.llm.client import resolve_model

        primary, fallback = resolve_models(None, None)
        base, _key, mid = resolve_model(primary)
        out.append(("模型凭证", True, f"{mid} @ {base}（配置名={primary}，fallback={fallback}）"))
    except Exception as e:
        out.append(
            (
                "模型凭证",
                False,
                f"{e}；设置 SECPT_MODEL_API_KEY/DEEPSEEK_API_KEY/OPENAI_API_KEY，"
                "或 SECPT_MODEL_KEY_<PREFIX>；自定义端点另配 SECPT_MODEL_BASE_URL/NAME",
            )
        )

    # 3. var 可写
    var = Path(os.environ.get("SECPT_VAR", "var"))
    try:
        var.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=str(var), prefix=".doctor-"):
            pass
        out.append(("var/ 可写", True, str(var.resolve())))
    except Exception as e:
        out.append(("var/ 可写", False, f"{var}: {e}"))

    # 4. docker / 沙箱（SECPT_SANDBOX=0 时仅提示）
    if os.environ.get("SECPT_SANDBOX", "1") == "0":
        out.append(("Docker 沙箱", True, "已全局关闭（SECPT_SANDBOX=0，宿主直跑）"))
    else:
        try:
            from secpt.runtime.sandbox import FALLBACK_IMAGE, docker_available

            if docker_available():
                img = os.environ.get("SECPT_SANDBOX_IMAGE", "secpt-sandbox")
                exists = shutil.which("docker") is not None
                out.append(
                    ("Docker 沙箱", exists, f"可用；镜像 {img}（缺失回退 {FALLBACK_IMAGE}）")
                )
            else:
                out.append(("Docker 沙箱", False, "docker 不可用——bash 将回退宿主执行（风险自担）"))
        except Exception as e:
            out.append(("Docker 沙箱", False, str(e)))

    # 5. 外部工具矩阵（缺失 = 警告不阻断）
    for name, bin_name, purpose in _TOOLS:
        path = shutil.which(bin_name)
        out.append((f"工具 {name}", path is not None, path or f"未安装（{purpose}）"))

    # 6. 可选 Python 依赖
    import importlib.util

    for mod, hint in _OPTIONAL_PY:
        found = importlib.util.find_spec(mod) is not None
        out.append((f"可选 {mod}", found, "已安装" if found else hint))

    # 7. VPN / benchmark（按需）
    vpn_cfg = os.environ.get("SECPT_VPN_CONFIG", "")
    out.append(("VPN 配置", bool(vpn_cfg), vpn_cfg or "未配置（--vpn 需要 SECPT_VPN_CONFIG）"))
    bench = bool(os.environ.get("BENCHMARK_BASE_URL") and os.environ.get("BENCHMARK_TOKEN"))
    out.append(("benchmark 凭证", bench, "已配置" if bench else "未配置（benchmark 跑分需要）"))

    return out


def doctor_cmd() -> None:
    """环境预检：模型凭证 / 沙箱 / 工具矩阵 / 可选依赖，逐项给修复提示。"""
    results = run_checks()
    width = max(len(name) for name, _ok, _d in results)
    failures = 0
    typer.echo(f"{'检查项':<{width}}  结果  详情")
    typer.echo("-" * (width + 40))
    for name, ok, detail in results:
        mark = "✓" if ok else "✗"
        if not ok:
            failures += 1
        typer.echo(f"{name:<{width}}  {mark}     {detail}")
    typer.echo("")
    if failures:
        typer.echo(f"共 {failures} 项需要关注（✗ 大多为可选组件，按详情提示处理即可）")
    else:
        typer.echo("全部通过，环境就绪。")
