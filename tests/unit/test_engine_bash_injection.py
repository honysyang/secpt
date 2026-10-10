class TestCoreToolsNotInWhitelist:
    """回归：profile tools 里的内置工具名不得并入 bash 白名单。

    author_plus 真实运行暴露：'bash' 被误并入白名单导致 bash -lc 放开、
    模型用 shell 拼相对路径读附件。
    """

    def test_bash_extra_excludes_core_tools(self):
        from secpt.core.profile.loader import CORE_TOOLS, load_profile

        prof = load_profile("ctf")
        assert "bash" in prof.tools  # profile 声明了内置工具
        assert "bash" in CORE_TOOLS  # bash 是内置工具名（非 shell 白名单成员）
        assert {"bash", "read_file", "list_dir", "write_file"} <= CORE_TOOLS
        # 白名单逻辑必须排除 CORE_TOOLS（回归：误并入会放开 bash -lc）
        bash_extra = [t for t in prof.tools if t not in CORE_TOOLS]
        assert "bash" not in bash_extra and "read_file" not in bash_extra
