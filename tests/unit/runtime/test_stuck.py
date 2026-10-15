"""StuckDetector 进展指纹（P3-11）单元测试。

覆盖三种场景：重复参数爆破不误杀 / 随机噪声不计进展 / pwn 合法交互不误杀，
以及归一化与结构签名的单元断言。
"""

from secpt.runtime.stuck import StuckDetector, _normalize, _signature


def test_identical_repeat_is_waiting_not_progress():
    """连续完全相同输出（pwn 等待/banner 重印）：不触发 nudge，超 waiting 上限才提醒。"""
    det = StuckDetector()  # threshold 6, wait_cap 12
    out = "=== MENU ===\n1. send payload\n2. quit\n$ "
    assert det.update(out) is True  # 冷启动视为进展
    assert [det.update(out) for _ in range(9)] == [False] * 9
    assert det.should_nudge() is False  # waiting=9 < 12，不误杀
    for _ in range(3):
        det.update(out)  # waiting=12
    assert det.should_nudge() is True  # 超过 waiting 上限才提醒


def test_param_change_same_path_not_progress():
    """同端点换 query 参数：URL 归一化后不算新信息（旧逻辑会因新 URL 误判进展）。"""
    det = StuckDetector()
    det.update("http://t/note?id=a\nnote: 200 OK")
    for i in range(8):
        ret = det.update(f"http://t/note?id={i}\nnote: 200 OK")
        assert ret is False
    assert det.should_nudge() is False  # waiting 未超上限


def test_random_session_noise_not_progress():
    """响应体每次随机（32 位 hex 会话 id）：归一化后签名稳定，不算进展（_seen 不膨胀）。"""
    det = StuckDetector()
    det.update("session 4c2f9d1a8b3e7c5d heartbeat ok http://t/a")
    for i in range(8):
        sid = f"{i:032x}"
        ret = det.update(f"session {sid} heartbeat ok http://t/a")
        assert ret is False
    assert det.should_nudge() is False


def test_structural_new_info_is_progress():
    """新路径 / 新状态码 / 新字段出现 → 计进展并重置计数。"""
    det = StuckDetector()
    det.update("http://t/ HTTP 200 <html>")
    assert det.update("http://t/admin HTTP 403") is True  # 新路径 + 新状态码
    assert det.update("http://t/admin HTTP 403") is False  # 重复不算
    assert det.update('{"admin": true, "role": "root"}') is True  # 新字段


def test_menu_loop_capped_waiting():
    """菜单循环（两个屏幕交替）：走 waiting 状态机，超过上限才提醒。"""
    det = StuckDetector(wait_cap=4)
    a = "=== MENU ===\n[1] leak\n[2] quit\n> "
    b = "=== MENU ===\n[1] leak\n[2] quit\n> 1\nsending"
    det.update(a)  # 冷启动
    assert det.update(b) is True  # 新内容 → 进展
    assert det.update(a) is False  # 重印 → waiting
    assert det.update(b) is False
    assert det.should_nudge() is False  # waiting=2 < 4
    det.update(a)
    det.update(b)  # waiting=4
    assert det.should_nudge() is True


def test_normalize_strips_noise():
    """归一化：时间戳 / hex 大数 / 长数字 / 随机 id 全部脱敏。"""
    assert (
        _normalize("2026-10-13 10:00:01 sid=0xdeadbeefcafebabe 12345678") == "<TS> sid=<HEX> <NUM>"
    )


def test_signature_strips_query_and_random_path_id():
    """URL 签名：query 参数与随机路径 id 变化不算新信息，新路径才算。"""
    s1 = _signature("http://t/note?id=abc\nOK")
    s2 = _signature("http://t/note?id=def\nOK")
    assert s1 == s2
    s3 = _signature("http://t/note/4c2f9d1a8b3e7c5d\nOK")
    s4 = _signature("http://t/note/9e3b7c5d1a8b4f2c\nOK")
    assert s3 == s4
    s5 = _signature("http://t/admin\nOK")
    assert s5 != s3  # 新路径 → 签名不同


# ---------------- P3 同类报错连续指纹 ----------------


def test_same_error_streak_triggers_early():
    """同类报错连续 ≥3 次直接触发（不等 no_progress 达 6 步阈值）——
    覆盖纯试错循环：报错根因不变（第一行错误串完全一致），但每次响应带
    不同附加行（请求耗时/trace id 等噪声），结构签名视为"有进展"，
    旧逻辑要等 6 步无进展才提醒，新逻辑第 3 次同因报错即触发。"""
    det = StuckDetector(threshold=6)  # no_progress 阈值 6（正常路径不触发）
    for i in range(3):
        out = (
            "Error: start failed: max active container limit reached\n"
            f"attempt={i} latency={i * 7}ms trace-step-{i + 1:08d}"
        )
        ret = det.update(out)
        assert ret is True  # 附加行不同 → 结构签名有新增（旧逻辑无法识别卡死）
    assert det.should_nudge() is True  # err_streak=3 → 触发
    msg = det.nudge()
    assert "同一类报错" in msg  # 触发的是"换根因"文案而非"换层"文案
    assert det.trigger_count == 1
    assert det.should_nudge() is False  # 触发后 streak 重置，不刷屏


def test_different_error_resets_streak():
    """错误根因变化（不同指纹）→ streak 重置，不触发。"""
    det = StuckDetector(threshold=6)
    det.update("Error: connection refused")
    det.update("Error: permission denied")  # 指纹不同 → streak 重置为 1
    det.update("Error: connection refused")  # 又不同 → 重置为 1
    assert det.should_nudge() is False


def test_error_fingerprint_normalizes_params():
    """报错指纹归一化：6+ 位数字 / 16+ 位 hex id 脱敏 → 同因不同参同指纹。"""
    a = StuckDetector._error_fingerprint("Error: quota 123456789 exceeded req 4c2f9d1a8b3e7c5d")
    b = StuckDetector._error_fingerprint("Error: quota 987654321 exceeded req 0000000000000000")
    assert a == b and a  # 参数不同但根因同类 → 同指纹
    c = StuckDetector._error_fingerprint("no error here")
    assert c == ""


# ---------------- ⑥ 完全重复调用（同工具同参数、结果不变） ----------------


def test_duplicate_call_triggers_on_third_identical():
    """同工具同参数且不产生新信息：第 3 次即触发（比"无新信息"6 步阈值更早、
    更可行动）。1053 实测 api.py holders 连调 3 次即此类空转。"""
    det = StuckDetector()
    key = 'bash:{"command": "python3 api.py holders"}'
    det.update('{"holders": {}}', call_key=key)  # 冷启动视为进展
    assert det.should_nudge() is False
    det.update('{"holders": {}}', call_key=key)  # 重复且无新信息（1）
    assert det.should_nudge() is False
    det.update('{"holders": {}}', call_key=key)  # 重复且无新信息（2）→ 触发
    assert det.should_nudge() is True
    msg = det.nudge()
    assert "完全相同的工具和参数" in msg
    assert det.trigger_count == 1
    assert det.should_nudge() is False  # 触发后清空调用窗口，不刷屏


def test_duplicate_call_with_new_info_not_triggered():
    """同命令但每次产出新信息（新路径/新端点）→ 不触发，避免误伤合法轮询。"""
    det = StuckDetector()
    key = 'bash:{"command": "cat app.log"}'
    for i in range(6):
        assert det.update(f"GET http://t/u{i} 200", call_key=key) is True
    assert det.should_nudge() is False


def test_duplicate_call_oscillation_interleaved():
    """两个命令交替空转（A/B/A/B/A）：各自累计无新信息重复 → 触发（覆盖
    1053 的 holders/list 交替探测模式）。"""
    det = StuckDetector()
    ka = 'bash:{"command": "python3 api.py start a"}'
    kb = 'bash:{"command": "python3 api.py start b"}'
    det.update('{"error": 409}', call_key=ka)  # 冷启动
    det.update('{"error": 404}', call_key=kb)  # 新状态码 → 进展
    det.update('{"error": 409}', call_key=ka)  # 无新信息（1）
    det.update('{"error": 404}', call_key=kb)  # 无新信息（1）
    det.update('{"error": 409}', call_key=ka)  # 无新信息（2）→ 触发
    assert det.should_nudge() is True


def test_no_call_key_keeps_legacy_semantics():
    """不传 call_key（旧调用方/其他单测）：仅走原有进展指纹，不因"重复调用"提前触发。"""
    det = StuckDetector()
    out = "=== MENU ===\n1. send\n2. quit\n$ "
    det.update(out)
    for _ in range(9):
        det.update(out)  # 完全相同的输出，但不传 call_key
    assert det.should_nudge() is False  # waiting=9 < 12，行为与改造前一致


# ---------------- 节流（复评修复轮：冷却/退避/回落/上限/等待文案） ----------------


def test_cooldown_suppresses_retrigger():
    """S1：触发后 8 步冷却内同类触发器不再触发（旧实现每 3 步刷一条）。"""
    det = StuckDetector()
    key = 'bash:{"command": "ls"}'
    det.update("same", call_key=key)  # 冷启动
    det.update("same", call_key=key)
    det.update("same", call_key=key)  # dup=2 → 触发
    assert det.should_nudge() is True
    det.nudge()
    assert det.trigger_count == 1
    for i in range(7):  # 冷却期内继续打同一命令：恒不触发
        det.update("same", call_key=key)
        assert det.should_nudge() is False, i
    det.update("same", call_key=key)  # 冷却耗尽
    assert det.should_nudge() is True  # 退避后重新触发（门槛已升）


def test_progress_decays_escalation():
    """S4：触发推高的阈值/退避，随进展逐级回落（防"只涨不落"检测失聪）。"""
    det = StuckDetector()
    for _ in range(3):
        det._waiting = det._wait_cap  # 直接构造触发条件（waiting 路径）
        det._cooldown = 0
        assert det.should_nudge() is True
        det.nudge()
    assert det._threshold == 6 + 2 * 3
    for i in range(20):
        det.update(f"new-path-{i} HTTP 200 /p{i}")  # 持续进展
    assert det._threshold == 6  # 回落到基线
    assert det._dup_cap_extra == 0 and det._err_cap_extra == 0


def test_session_nudge_cap():
    """S5：达 max_nudges 后不再触发（只记 trigger_count，不注水上下文）。"""
    det = StuckDetector(max_nudges=2)
    for _ in range(3):
        det._waiting = det._wait_cap
        det._cooldown = 0
        if det.should_nudge():
            det.nudge()
    assert det.trigger_count == 2
    det._waiting = det._wait_cap
    det._cooldown = 0
    assert det.should_nudge() is False  # 上限后静默


def test_waiting_specific_message():
    """S3：等待场景触发专属文案（此前复用"无新信息"文案，语义错位）。"""
    det = StuckDetector(wait_cap=3)
    out = "=== MENU ===\n1. send\n$ "
    det.update(out)
    for _ in range(3):
        det.update(out)
    assert det.should_nudge() is True
    msg = det.nudge()
    assert "等待/重印" in msg


def test_dup_escalation_raises_bar():
    """S2：dup 触发后退避——同类再犯需要更多证据（门槛 2→3）。"""
    det = StuckDetector(threshold=999)  # 关闭 no_progress 路径，隔离本测试
    ka = 'bash:{"command": "holders"}'
    for _ in range(3):
        det.update("same", call_key=ka)
    assert det.should_nudge() is True
    det.nudge()
    assert det._dup_cap_extra == 1
    det._cooldown = 0  # 直接跳过冷却（本测试只验证退避门槛）
    # 窗口已清空：重打 ka 需 3 次（退避后门槛 2+1）而非 2 次
    for i in range(3):
        det.update("same", call_key=ka)
        if i < 2:
            assert det.should_nudge() is False
    assert det.should_nudge() is True  # 第 3 次才达到退避后门槛
