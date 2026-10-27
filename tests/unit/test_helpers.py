"""共享工具函数单元测试。"""

from harness.utils.helpers import extract_steps, truncate


class TestExtractSteps:
    def test_numbered_list(self):
        text = "1. 第一步\n2. 第二步\n3. 第三步"
        assert extract_steps(text) == ["第一步", "第二步", "第三步"]

    def test_dash_list(self):
        text = "- 项目 A\n- 项目 B"
        assert extract_steps(text) == ["项目 A", "项目 B"]

    def test_plain_text(self):
        text = "没有编号的文本"
        assert extract_steps(text) == ["没有编号的文本"]


class TestTruncate:
    def test_short_text(self):
        assert truncate("短文本", 10) == "短文本"

    def test_long_text(self):
        result = truncate("很长的文本内容", 5)
        assert result == "很长的文本..."
