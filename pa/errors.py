"""secpt 异常分层（BUILD_SPEC §17.5；ARCHITECTURE 附录 A 命名约定）。

所有模块只允许抛 SecptError 及其子类，禁止抛裸 Exception。
"""


class SecptError(Exception):
    """secpt 全部异常的基类。"""


class PolicyViolation(SecptError):
    """策略拦截：越界目标、风险档超限、能力子集外的工具等。"""


class ModelParseError(SecptError):
    """模型输出解析/校验失败（BUILD_SPEC §7.2 / §11.2）。与 ModelCallError
    区分：前者是"话说错了"，后者是"没接上话"——重试与判责路径不同。"""


class ModelCallError(SecptError):
    """模型调用失败：网络、鉴权、响应结构异常等（S7 追加）。

    异常信息里不得携带 api_key。
    """


class BudgetExceeded(SecptError):
    """预算 100% 硬停（BUILD_SPEC §6.7）。"""


class IllegalTransition(SecptError):
    """任务状态机非法转移（ARCHITECTURE §6.1：抛错而非静默修正）。"""
