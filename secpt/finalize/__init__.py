"""收尾层（M1 件：W2-1 终场/半场报告与出场三验）。

分层红线：core 不 import 本包（依赖倒置，core/loop.py 只约定 ExitVerdict
形状与 ExitChecker 协议，装配方从最外层注入 ExitChecklist 实例）。
"""
