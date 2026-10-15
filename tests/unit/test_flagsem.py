"""P0-1 成果语义：真实 flag 甄别（extract_real_flags / is_real_flag）。"""

from __future__ import annotations

from secpt.core.orchestrator.flagsem import extract_real_flags, is_real_flag


class TestExtractRealFlags:
    def test_benchmark_settlement_not_flag(self):
        """benchmark 结算摘要（unique_code 前缀 + 动作串）→ 不计为 flag。"""
        ans = "unique_code=g-05 已拿到 2 个 flag 并 close，本轮得分 100"
        assert extract_real_flags(ans) == []

    def test_flag_form_extracted(self):
        """主流花括号前缀形态 → 提取本体。"""
        assert extract_real_flags("FLAG{pwned}") == ["FLAG{pwned}"]
        assert extract_real_flags("got RCTF{abc_123} ok") == ["RCTF{abc_123}"]
        assert extract_real_flags("flag{xyz}") == ["flag{xyz}"]

    def test_flag_assign_form(self):
        """无花括号赛制：flag=xxx / flag:xxx 形态提取。"""
        assert extract_real_flags("flag=abc123def") == ["flag=abc123def"]
        assert extract_real_flags("Flag: deadbeef") == ["Flag: deadbeef"]

    def test_dedup_and_empty(self):
        assert extract_real_flags("FLAG{dup} FLAG{dup}") == ["FLAG{dup}"]
        assert extract_real_flags("") == []
        assert extract_real_flags(None) == []

    def test_plain_summary_not_flag(self):
        """普通结论摘要（无 flag 形态）→ 空。"""
        assert extract_real_flags("RECON_DONE") == []
        assert extract_real_flags("拿到 shell，未见 flag") == []


class TestIsRealFlag:
    def test_single_token_flag(self):
        assert is_real_flag("FLAG{pwned}")
        assert is_real_flag("RCTF{abc}")

    def test_multi_token_is_summary(self):
        """整串含空白 → 是结论摘要，不是 flag 本体。"""
        assert not is_real_flag("FLAG{pwned} 已确认")
        assert not is_real_flag("unique_code=g-05 完成")
        assert not is_real_flag("")
