"""工具体系测试：@tool 装饰器、内置工具集、脚本工具加载器。"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness.tools.builtin import register_builtin
from harness.tools.decorator import tool
from harness.tools.loader import ScriptToolLoader
from harness.tools.registry import ToolRegistry


# ---------- @tool 装饰器 ----------


class TestToolDecorator:
    """装饰器 Schema 生成测试。"""

    def test_schema_from_signature(self):
        @tool
        def sample(city: str, days: int = 3, verbose: bool = False) -> str:
            """查询天气预报。

            Args:
                city: 城市名
                days: 预报天数
            """
            return "ok"

        registry = ToolRegistry()
        registry.register_function(sample)

        assert "sample" in registry.list_tools()
        schema = registry.get("sample").parameters  # type: ignore[union-attr]
        assert schema["properties"]["city"]["type"] == "string"
        assert schema["properties"]["city"]["description"] == "城市名"
        assert schema["properties"]["days"]["default"] == 3
        assert schema["required"] == ["city"]

    def test_tool_with_custom_name_and_dangerous(self):
        @tool(name="calc", dangerous=True)
        def anything(expression: str) -> str:
            """计算表达式。"""
            return "0"

        registry = ToolRegistry()
        registry.register_function(anything)
        registered = registry.get("calc")
        assert registered is not None
        assert registered.dangerous is True

    def test_register_undecorated_function_raises(self):
        registry = ToolRegistry()

        def plain(x: int) -> int:
            return x

        with pytest.raises(TypeError, match="未被 @tool 装饰"):
            registry.register_function(plain)

    def test_summary_from_docstring(self):
        @tool
        def with_long_doc(path: str) -> str:
            """读取文件。

            更详细的多段说明不应出现在描述里。

            Args:
                path: 路径
            """
            return path

        spec = with_long_doc.__tool_spec__  # type: ignore[attr-defined]
        assert spec["description"] == "读取文件。"


# ---------- 内置工具 ----------


class TestBuiltinRegistration:
    """内置工具注册测试。"""

    def test_register_builtin_all(self):
        registry = ToolRegistry()
        names = register_builtin(registry)

        expected = {
            "get_time", "calculate",
            "read_file", "write_file", "list_dir", "search_text",
            "run_command", "python_script",
            "http_request",
        }
        assert expected <= set(names)
        # 危险工具被正确标记
        dangerous = set(registry.get_dangerous_tools())
        assert {"run_command", "write_file", "python_script"} <= dangerous

    def test_register_builtin_readonly_mode(self):
        """exclude dangerous 时跳过危险工具。"""
        registry = ToolRegistry()
        names = register_builtin(registry, include_dangerous=False)
        assert "run_command" not in names
        assert "write_file" not in names
        assert "read_file" in names


class TestBuiltinBase:
    """时间与计算器测试。"""

    async def test_get_time(self):
        registry = ToolRegistry()
        register_builtin(registry)
        result = await registry.get("get_time").execute()  # type: ignore[union-attr]
        assert "T" not in result or "-" in result  # 含日期
        assert len(result) >= 10

    async def test_calculate_normal(self):
        registry = ToolRegistry()
        register_builtin(registry)
        tool = registry.get("calculate")
        assert await tool.execute(expression="(2+3)*4") == "20"  # type: ignore[union-attr]
        assert await tool.execute(expression="2**10") == "1024"  # type: ignore[union-attr]
        assert await tool.execute(expression="round(pi, 2)") == "3.14"  # type: ignore[union-attr]

    async def test_calculate_rejects_code_injection(self):
        """AST 白名单必须拒绝 eval 类注入。"""
        registry = ToolRegistry()
        register_builtin(registry)
        tool = registry.get("calculate")
        result = await tool.execute(expression="__import__('os').system('id')")  # type: ignore[union-attr]
        assert result.startswith("计算失败")

    async def test_calculate_rejects_huge_exponent(self):
        """幂指数上限防止资源耗尽。"""
        registry = ToolRegistry()
        register_builtin(registry)
        result = await registry.get("calculate").execute(expression="9**99999")  # type: ignore[union-attr]
        assert result.startswith("计算失败")


class TestBuiltinFs:
    """文件工具测试。"""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        register_builtin(reg)
        return reg

    async def test_write_read_roundtrip(self, registry: ToolRegistry, tmp_path: Path):
        target = tmp_path / "sub" / "note.txt"
        await registry.get("write_file").execute(path=str(target), content="hello 工具")  # type: ignore[union-attr]
        content = await registry.get("read_file").execute(path=str(target))  # type: ignore[union-attr]
        assert "hello 工具" in content

    async def test_read_missing_file(self, registry: ToolRegistry):
        result = await registry.get("read_file").execute(path="/no/such/file")  # type: ignore[union-attr]
        assert result.startswith("错误")

    async def test_list_dir_and_search(self, registry: ToolRegistry, tmp_path: Path):
        (tmp_path / "a.txt").write_text("alpha needle beta\n", encoding="utf-8")
        (tmp_path / "b.txt").write_text("nothing here\n", encoding="utf-8")

        listing = await registry.get("list_dir").execute(path=str(tmp_path))  # type: ignore[union-attr]
        assert "a.txt" in listing and "b.txt" in listing

        hits = await registry.get("search_text").execute(  # type: ignore[union-attr]
            path=str(tmp_path), pattern="needle"
        )
        assert "a.txt:1" in hits


class TestBuiltinShell:
    """命令执行工具测试（安全边界是重点）。"""

    @pytest.fixture
    def registry(self) -> ToolRegistry:
        reg = ToolRegistry()
        register_builtin(reg)
        return reg

    async def test_run_command_string_and_list(self, registry: ToolRegistry):
        tool = registry.get("run_command")
        result = await tool.execute(command="echo hello")  # type: ignore[union-attr]
        assert "hello" in result and "exit_code: 0" in result

        result = await tool.execute(command=["echo", "argv", "mode"])  # type: ignore[union-attr]
        assert "argv mode" in result

    async def test_run_command_rejects_control_chars(self, registry: ToolRegistry):
        """换行注入被拒绝。"""
        result = await registry.get("run_command").execute(command=["echo", "a\nb"])  # type: ignore[union-attr]
        assert result.startswith("错误")

    async def test_run_command_unknown_binary(self, registry: ToolRegistry):
        result = await registry.get("run_command").execute(command="definitely_not_a_cmd_xyz")  # type: ignore[union-attr]
        assert result.startswith("错误")

    async def test_python_script_missing(self, registry: ToolRegistry):
        result = await registry.get("python_script").execute(path="/no/such.py")  # type: ignore[union-attr]
        assert result.startswith("错误")

    async def test_python_script_runs(self, registry: ToolRegistry, tmp_path: Path):
        script = tmp_path / "ok.py"
        script.write_text("print('script ok')\n", encoding="utf-8")
        result = await registry.get("python_script").execute(path=str(script))  # type: ignore[union-attr]
        assert "script ok" in result


class TestBuiltinWeb:
    """HTTP 工具测试（不依赖外网，只测安全边界）。"""

    async def test_rejects_non_http_scheme(self):
        registry = ToolRegistry()
        register_builtin(registry)
        result = await registry.get("http_request").execute(url="file:///etc/passwd")  # type: ignore[union-attr]
        assert result.startswith("错误")

    async def test_rejects_empty_scheme(self):
        registry = ToolRegistry()
        register_builtin(registry)
        result = await registry.get("http_request").execute(url="not-a-url")  # type: ignore[union-attr]
        assert result.startswith("错误")


# ---------- 脚本工具加载器 ----------


class TestScriptToolLoader:
    """脚本工具加载测试。"""

    def _make_script(self, directory: Path, filename: str, body: str) -> Path:
        script = directory / filename
        script.write_text(body, encoding="utf-8")
        return script

    def test_load_execute_convention(self, tmp_path: Path):
        self._make_script(tmp_path, "weather.py", '''
TOOL_NAME = "weather"
TOOL_DESCRIPTION = "查询城市天气"

def execute(city: str, days: int = 3):
    return f"{city} {days}天晴"
''')
        registry = ToolRegistry()
        loaded = ScriptToolLoader(tmp_path).load_into(registry)

        assert loaded == ["weather"]
        tool_obj = registry.get("weather")
        assert tool_obj is not None
        assert tool_obj.description == "查询城市天气"
        # 参数 Schema 从 execute 签名推断
        assert tool_obj.parameters["properties"]["city"]["type"] == "string"

    async def test_loaded_async_script_executes(self, tmp_path: Path):
        self._make_script(tmp_path, "async_tool.py", '''
async def execute(x: int):
    return x * 2
''')
        registry = ToolRegistry()
        ScriptToolLoader(tmp_path).load_into(registry)

        tool_obj = registry.get("async_tool")
        assert tool_obj is not None
        result = await tool_obj.execute(x=21)
        assert result == 42

    def test_load_decorator_style_script(self, tmp_path: Path):
        self._make_script(tmp_path, "decorated.py", '''
from harness.tools.decorator import tool

@tool(name="greet", dangerous=True)
def execute(name: str):
    """向某人问好。"""
    return f"hi {name}"
''')
        registry = ToolRegistry()
        loaded = ScriptToolLoader(tmp_path).load_into(registry)

        assert loaded == ["greet"]
        assert registry.get("greet").dangerous is True  # type: ignore[union-attr]

    def test_broken_script_skipped(self, tmp_path: Path):
        """坏脚本只告警跳过，不影响其他脚本（静默降级）。"""
        self._make_script(tmp_path, "broken.py", "raise RuntimeError('boom')\n")
        self._make_script(tmp_path, "good.py", "def execute():\n    return 'ok'\n")

        registry = ToolRegistry()
        loaded = ScriptToolLoader(tmp_path).load_into(registry)
        assert loaded == ["good"]

    def test_script_without_execute_skipped(self, tmp_path: Path):
        self._make_script(tmp_path, "no_tool.py", "X = 1\n")
        registry = ToolRegistry()
        loaded = ScriptToolLoader(tmp_path).load_into(registry)
        assert loaded == []

    def test_name_conflict_not_overwritten(self, tmp_path: Path):
        self._make_script(tmp_path, "clash.py", '''
TOOL_NAME = "get_time"
def execute():
    return "fake"
''')
        registry = ToolRegistry()
        register_builtin(registry)
        loaded = ScriptToolLoader(tmp_path).load_into(registry)

        assert loaded == []  # 冲突跳过
        result = asyncio.run(registry.get("get_time").execute())
        assert "fake" not in result  # 原工具未被覆盖

    def test_underscore_files_ignored(self, tmp_path: Path):
        self._make_script(tmp_path, "_helper.py", "def execute():\n    return 1\n")
        registry = ToolRegistry()
        loaded = ScriptToolLoader(tmp_path).load_into(registry)
        assert loaded == []

    def test_missing_directory_returns_empty(self, tmp_path: Path):
        loader = ScriptToolLoader(tmp_path / "not_exist")
        assert loader.load_into(ToolRegistry()) == []


class TestRunCommandShellMeta:
    """复合命令预检：拒绝 shell 语法，禁止静默语义错误。"""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("command", [
        "echo '--- DNS ---'; getent hosts example.com",
        "cat a.txt && cat b.txt",
        "curl -s https://x.com | grep title",
        "ls > /tmp/out.txt",
        "echo $(whoami)",
        "for p in a b; do echo $p; done",
    ])
    async def test_rejects_shell_syntax(self, command: str):
        from harness.tools.builtin.shell import run_command

        result = await run_command(command=command)

        assert "不支持 shell 语法" in result
        assert "python_script" in result  # 错误信息引导替代方案

    @pytest.mark.asyncio
    async def test_plain_command_still_works(self):
        from harness.tools.builtin.shell import run_command

        result = await run_command(command=["echo", "hello"])
        assert "exit_code: 0" in result
        assert "hello" in result

    @pytest.mark.asyncio
    async def test_list_argv_bypasses_meta_check(self):
        """列表传参本就是显式 argv，不需要字符串预检。"""
        from harness.tools.builtin.shell import run_command

        result = await run_command(command=["echo", "a|b"])
        assert "exit_code: 0" in result
        assert "a|b" in result


class TestOutputPathRedirect:
    """纯文件名相对路径重定向到 outputs/，防止 LLM 产物污染工作目录根部。"""

    def test_bare_filename_redirected(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from harness.tools.builtin.fs import resolve_output_path

        resolved, redirected = resolve_output_path("scan.py")
        assert redirected is True
        assert resolved == Path("outputs/scan.py")

    def test_subdir_and_absolute_kept(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from harness.tools.builtin.fs import resolve_output_path

        assert resolve_output_path("tools/gen.py") == (Path("tools/gen.py"), False)
        assert resolve_output_path("/tmp/scan.py") == (Path("/tmp/scan.py"), False)
        # ./x.py 被 pathlib 规范化为 x.py —— 与裸文件名等价，同样重定向
        assert resolve_output_path("./x.py") == (Path("outputs/x.py"), True)

    @pytest.mark.asyncio
    async def test_write_file_redirects_bare_name(self, monkeypatch, tmp_path):
        monkeypatch.chdir(tmp_path)
        from harness.tools.builtin.fs import write_file

        result = write_file(path="gen.py", content="print(1)")

        assert "outputs" in result
        assert (tmp_path / "outputs" / "gen.py").is_file()
        assert not (tmp_path / "gen.py").exists()  # 项目根未被污染

    @pytest.mark.asyncio
    async def test_python_script_finds_redirected_script(self, monkeypatch, tmp_path):
        """write_file 重定向后的脚本，python_script 用纯文件名能找到并执行。"""
        monkeypatch.chdir(tmp_path)
        from harness.tools.builtin.fs import write_file
        from harness.tools.builtin.shell import python_script

        write_file(path="hello.py", content="print('from-outputs')")
        result = await python_script(path="hello.py")

        assert "exit_code: 0" in result
        assert "from-outputs" in result


class TestArgvLogFormat:
    """日志 argv 完整性：URL 等关键参数不被截掉。"""

    def test_url_at_tail_preserved(self):
        from harness.tools.builtin.shell import _argv_for_log

        argv = ["curl", "-sS", "-o", "/dev/null", "-D", "-", "--max-time", "20",
                "https://aimohu.com/"]
        logged = _argv_for_log(argv)
        assert logged[-1] == "https://aimohu.com/"  # 之前截到 8 个参数时 URL 恰好丢失

    def test_overlong_arg_truncated(self):
        from harness.tools.builtin.shell import _argv_for_log

        huge = "x" * 5000
        logged = _argv_for_log(["echo", huge])
        assert len(logged[1]) == 200  # 单参数上限，防日志爆炸
