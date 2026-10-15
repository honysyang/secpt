"""卡死切换器（StuckDetector）。

原样搬运自 runtime/harness.py，阈值改由调用方传入。
提醒消息以 user 角色注入（DeepSeek 对中途 system 消息处理不稳定）。

进展判据（P3-11 进展指纹）：输出结构签名相对最近 k 步滚动窗口有新增才算进展。
随机噪声不计入，合法重复交互（pwn 等待/菜单循环）走 waiting 状态机不误杀。
"""

from __future__ import annotations

import re
from collections import deque

FLAG_RE = re.compile(r"[Ff][Ll][Aa][Gg]\{[^{}]{3,}\}")

# 归一化噪声正则：时间戳 / 十六进制大数 / 十进制大数 / 随机长 id
_TS_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?\b")
_HEX_RE = re.compile(r"\b0x[0-9a-fA-F]{8,}\b")
_BIG_NUM_RE = re.compile(r"\b\d{6,}\b")
_RANDOM_ID_RE = re.compile(r"\b[0-9a-fA-F]{16,}\b")  # 会话/note 随机 id
_LINE_HEAD = 40  # 结构指纹取每行前 40 字符


def _normalize(text: str) -> str:
    """归一化：去随机 token、URL 只留路径、折叠空白、去重复行（banner/菜单重印只算一次）。"""
    t = _TS_RE.sub("<TS>", text)
    t = _HEX_RE.sub("<HEX>", t)
    t = _RANDOM_ID_RE.sub("<ID>", t)  # 先于大数：16+ 位随机 id（含纯数字）统一脱敏
    t = _BIG_NUM_RE.sub("<NUM>", t)
    # URL 只保留路径（query 参数变化不算新信息）
    t = re.sub(r"https?://[^\s\"'<>]+", lambda m: re.split(r"[?#]", m.group(0))[0], t)
    t = re.sub(r"[ \t]+", " ", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    seen: set[str] = set()
    uniq: list[str] = []
    for ln in t.split("\n"):
        ln = ln.strip()
        if ln and ln not in seen:
            seen.add(ln)
            uniq.append(ln)
    return "\n".join(uniq)


def _signature(text: str) -> frozenset[str]:
    """输出结构签名：结构关键词（URL/路径/状态码/字段/FLAG/错误）+ 归一化行首指纹。

    URL 只保留路径部分且再归一化（query 参数与随机路径 id 变化不算新信息，
    避免同端点换参数/随机会话被误判进展）。
    """
    sig: set[str] = set()
    for kw in extract_keywords(text):
        if kw.startswith("http"):
            sig.add(f"U:{_normalize(re.split(r'[?#]', kw)[0])}")
        elif kw.startswith("/"):
            sig.add(f"P:{_normalize(kw)}")  # 路径（含随机 id）也归一化
        else:
            sig.add(kw)
    for ln in _normalize(text).split("\n"):
        if ln:
            sig.add(f"L:{ln[:_LINE_HEAD]}")
    return frozenset(sig)


def extract_keywords(text: str) -> set[str]:
    """从工具输出中提取关键词（文件路径、URL、HTTP 状态码、字段名）。"""
    kws: set[str] = set()
    # URL
    for m in re.finditer(r"https?://[^\s\"'<>]+", text):
        kws.add(m.group(0))
    # 文件路径
    for m in re.finditer(r"(?:/[\w.\-]+){2,}", text):
        kws.add(m.group(0))
    # HTTP 状态码
    for m in re.finditer(r"\b\d{3}\b", text):
        code = int(m.group(0))
        if 100 <= code < 600:
            kws.add(f"HTTP_{code}")
    # 字段名（JSON key 或 HTML 属性）
    for m in re.finditer(r'"([\w_]+)"\s*:', text):
        kws.add(f"field:{m.group(1)}")
    # flag 命中
    for m in FLAG_RE.finditer(text):
        kws.add(f"FLAG:{m.group(0)}")
    # 错误关键词
    for kw in (
        "timeout",
        "Connection refused",
        "401",
        "403",
        "404",
        "500",
        "Access Denied",
        "Permission denied",
        "not found",
    ):
        if kw.lower() in text.lower():
            kws.add(f"err:{kw}")
    return kws


class StuckDetector:
    """卡死切换器：连续无结构新信息时注入提醒。

    进展 = 输出结构签名相对最近 k 步（滚动窗口）有新增。
    合法重复交互（pwn 等待/菜单循环）走 waiting 计数，不累计无进展；
    只有 waiting 超过 wait_cap 或无进展超过 threshold 才触发提醒。
    P3 指纹增强：同类报错连续 ≥3 次直接触发（覆盖纯试错循环——每次参数略变
    使签名有"新增"，但真实根因是同一错误类，如连续撞 max active 限流）。
    """

    # 同类报错指纹提取：取错误行首段做归一化（数字/时间戳/随机 id 脱敏）
    _ERR_LINE_RE = re.compile(r"^\s*(?:Error|error|ERROR|错误)[:：]", re.MULTILINE)
    # P3 ⑤：滑动窗口口径的同类错误输入定义——治理类拒绝（元字符/白名单/限速）
    # 也算错误输入；环境类输出（如 409 body 无 Error: 前缀）由调用方无须特判
    _ERR_INPUT_RE = re.compile(
        r"^\s*(?:Error|error|ERROR|错误)[:：]|命令含未引用的 shell 组合构造|不在白名单",
        re.MULTILINE,
    )

    def __init__(
        self,
        threshold: int = 6,
        increment: int = 2,
        window: int = 3,
        wait_cap: int = 12,
        err_streak_cap: int = 3,  # 同类报错触发阈值（P3 指纹增强）
        err_window: int = 8,  # ⑤ 滑动窗口：最近 N 步内同类错误计数窗口
        dup_cap: int = 2,  # 完全重复调用（同工具同参数且均无新信息）触发阈值
        dup_window: int = 8,  # 重复调用识别窗口（最近 N 次调用；取宽以覆盖 holders/list 交替）
        cooldown_steps: int = 8,  # 节流：任一触发后 N 步内不再触发（防刷屏）
        max_nudges: int = 20,  # 单会话 nudge 总量上限（超出只记事件不再注入上下文）
    ) -> None:
        self._window: deque[frozenset[str]] = deque(maxlen=window)
        self._no_progress = 0
        self._waiting = 0
        self._threshold = threshold
        self._base_threshold = threshold  # S4：进展回落的目标值
        self._increment = increment
        self._wait_cap = wait_cap
        self._err_streak_cap = err_streak_cap
        self._err_window_cap = err_window
        self._dup_cap = dup_cap
        self.trigger_count = 0
        self._err_streak = (
            0  # 由 _update_output 维护；初始化兜底（should_nudge 可先于 update 调用）
        )
        # 节流（S1-S5）：四类触发器统一冷却窗口；dup/err 触发后退避（同类再犯需
        # 更多证据）；进展出现时阈值与退避逐级回落（防"只涨不落"导致检测失聪）
        self._cooldown = 0
        self._cooldown_steps = cooldown_steps
        self._max_nudges = max_nudges
        self._dup_cap_extra = 0
        self._err_cap_extra = 0
        self._ever_progress = False  # 窗口内是否出现过结构性进展（waiting 前置条件）
        # ⑤ 同类报错滑动窗口：最近 N 步输入的 (指纹, 是否同类错误) 序列——
        #    治"同类错误被中间成功调用重置"（0931 实测 err_streak 连续口径 0 触发）
        self._recent_inputs: deque[tuple[str, bool]] = deque(maxlen=err_window)
        # ⑥ 完全重复调用窗口：最近 N 次调用的 (调用指纹, 是否带来新信息)——同工具
        #    同参数且都不产生新信息 = 结果不会变，比"无新信息"更早触发、更可行动
        #    （1053 实测：api.py holders ×3、write_file api.py ×3、explore 脚本 ×4）
        self._recent_calls: deque[tuple[str, bool]] = deque(maxlen=dup_window)
        self._dup_repeat = 0

    @staticmethod
    def _error_fingerprint(text: str) -> str:
        """报错指纹：提取错误行首段归一化（同因不同参 → 同指纹）。"""
        m = StuckDetector._ERR_LINE_RE.search(text or "")
        if not m:
            return ""
        tail = text[m.start() : m.start() + 160]
        return _normalize(tail.split("\n")[0])

    def update(self, tool_output: str, call_key: str | None = None) -> bool:
        """返回 True 表示有结构性新信息，False 表示无进展/等待/噪声。

        call_key：本步工具调用的指纹（工具名 + 规范化参数）。传入时额外做"完全重复
        调用"判定——引擎按原始顺序串行记账时逐条传入，口径与该方法一致。
        不传（旧调用方/单测）时仅走原有进展指纹逻辑，行为完全不变。
        """
        if self._cooldown > 0:
            self._cooldown -= 1  # S1：触发后冷却逐步消耗（任何输出都算消耗）
        has_new = self._update_output(tool_output)
        if has_new:
            # S4：进展出现 → 阈值与触发退避逐级回落（触发只涨不落会把检测器
            # 推到失聪：49 次触发后阈值≈104，真卡死反而报不出来）
            if self._threshold > self._base_threshold:
                self._threshold -= 1
            if self._dup_cap_extra > 0:
                self._dup_cap_extra -= 1
            if self._err_cap_extra > 0:
                self._err_cap_extra -= 1
        if call_key:
            self._note_call(call_key, has_new)
        return has_new

    def _note_call(self, call_key: str, has_new: bool) -> None:
        """记录一次调用；同指纹且都不带来新信息达（退避后的）阈值 → 置重复触发位。

        只统计"本次无新信息"的重复：合法重复调用若每次都产出新信息（如 pwn 轮询
        日志内容在变）不触发，避免误伤等待态（与 ⑤ 同类报错窗口口径一致）。
        阈值比较推迟到 should_nudge（用退避后的有效阈值），这里只维护计数。
        """
        self._recent_calls.append((call_key, has_new))
        self._dup_repeat = sum(1 for k, new in self._recent_calls if k == call_key and not new)

    def _update_output(self, tool_output: str) -> bool:
        """进展指纹主逻辑（原 update 主体，语义不变）。"""
        # ⑤ 滑动窗口计数：每个输入步记 (指纹, 是否错误)——
        # 中间成功调用只占窗口一格，不再清零同类错误计数
        is_err = bool(self._ERR_INPUT_RE.search(tool_output or ""))
        fp = self._error_fingerprint(tool_output) if is_err else ""
        self._recent_inputs.append((fp, is_err))
        # 窗口内同类错误计数：取窗口内出现次数最多的同指纹错误数——
        # 错误句式一致仅参数不同 → 归一化后同指纹；连续口径改为窗口口径后
        # 中间成功调用不再清零（占一格滑出窗口即可）
        same = [f for f, e in self._recent_inputs if e and f]
        if same:
            self._err_streak = max(same.count(fp0) for fp0 in set(same))
        else:
            self._err_streak = 0

        sig = _signature(tool_output)
        if not self._window:
            # 冷启动：前 k 步只积累窗口，全部视为进展
            self._window.append(sig)
            self._ever_progress = True
            return True
        recent = set().union(*self._window)
        if sig - recent:
            # 相对最近 k 步出现结构新增 → 进展（重置进展计数；错误窗口保留——
            # "成功穿插的同类错误"正是 ⑤ 要抓的纯试错循环）
            self._window.append(sig)
            self._ever_progress = True
            self._no_progress = 0
            self._waiting = 0
            return True
        if self._ever_progress and sig in self._window:
            # 最近窗口内出现过的签名（合法重印/菜单循环）→ waiting，不累计无进展
            self._waiting += 1
            return False
        # 结构无新增且不在窗口（内容在已知结构内换排列的噪声）→ 计无进展
        self._window.append(sig)
        self._no_progress += 1
        return False

    def should_nudge(self) -> bool:
        # 节流（S1-S5）：冷却窗口内 / 会话 nudge 达上限 → 一律不触发
        # （触发次数仍可由 trigger_count 观测；超出上限只记不注入，防上下文注水）
        if self._cooldown > 0 or self.trigger_count >= self._max_nudges:
            return False
        return (
            self._no_progress >= self._threshold
            or self._waiting >= self._wait_cap
            or self._err_streak >= self._err_streak_cap + self._err_cap_extra  # 退避后阈值
            or self._dup_repeat >= self._dup_cap + self._dup_cap_extra  # 退避后阈值
        )

    def nudge(self) -> str:
        """触发一次提醒：置冷却窗口 + 对应触发器退避 + 阈值 +increment。

        自 2026 复评节流轮：四类触发器共用冷却窗口（此前 dup/err 触发后清窗口
        即可立即再积累，同一命令连打会每 3 步刷一条——100 步实测 49 次触发）；
        dup/err 触发后退避（同类再犯需更多证据）；进展出现时逐级回落（update 内）。
        """
        self.trigger_count += 1
        self._threshold += self._increment
        self._no_progress = 0
        self._cooldown = self._cooldown_steps
        if self._dup_repeat >= self._dup_cap + self._dup_cap_extra:
            # ⑥ 完全重复调用触发：清空调用窗口 + 退避（同类再犯门槛 +1）
            n = self._dup_repeat
            self._dup_repeat = 0
            self._recent_calls.clear()
            self._dup_cap_extra += 1
            self._waiting = 0
            return (
                f"你已经用完全相同的工具和参数调用了 {n} 次，结果不会变。\n"
                "重复同一条命令只会得到同一个答案——先说明你期待它返回什么、"
                "为什么前几次没得到，再换一个真正不同信息的来源（换命令 / 换路径 / "
                "换目标端点 / 直接改用 read_file 看已有产物）。"
            )
        if self._err_streak >= self._err_streak_cap + self._err_cap_extra:
            # 同类报错触发：清空错误窗口 + 退避（同因再犯门槛 +1）
            self._err_streak = 0
            self._recent_inputs.clear()
            self._err_cap_extra += 1
            self._waiting = 0
            return (
                "你已连续多次触发同一类报错（错误根因未变，只是换了参数）。\n"
                "不要在同一根因上继续微调参数——先定位报错的真实原因：\n"
                "是权限/额度/网络不通，还是输入格式不对？确认后再换一条完全不同的路径重试。"
            )
        if self._waiting >= self._wait_cap:
            # 等待/重印场景专属文案（S3 前这里复用"无新信息"文案，语义错位）
            self._waiting = 0
            return (
                "你已经在一个等待/重印场景（菜单循环、任务等待或 banner 复读）停留很久了。\n"
                "先确认三件事：这个等待是否真的还会返回新信息？有没有更快的路径可以绕过它？"
                "如果确实只能等待，把当前状态用 record_fact 记下来再继续等；"
                "否则换一层：读源码 / 换会话 / 换协议字段 / 换目标端点。"
            )
        self._waiting = 0
        return (
            "你已经连续多步没有获得新信息了。换一层再试，不要在同一层继续换参数。\n"
            "可选的层：读源码 / 猜凭据 / 换身份或会话 / 换协议字段 / 换目标端点。\n"
            "先说清楚你现在在哪一层、为什么卡住，再决定换到哪一层。"
        )
