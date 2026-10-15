"""可选语义向量层（B：语义检索的公共底座）。

三套记忆（learning 经验 / intel 图 / traces）此前全是字面匹配——"命令执行"
检索不到 "RCE"，语义鸿沟砍掉了"观察交叉点"的大半。本模块提供可插拔嵌入：

- 提供者链：显式注入（set_embedder，测试/自定义）> fastembed（可选依赖，
  本地 ONNX，pip install 'secpt[embed]'）> None（降级回字面检索）。
  任何加载/推理失败一次告警后返 None，**绝不抛**（AGENTS 红线 5：学习/记忆
  是 best-effort，观测层不得打断主链路）。
- SECPT_EMBED=0 全局关闭。
- 缓存：文本 sha1 → 向量，落盘 var/knowledge/embed-cache.json（tmp+replace
  原子写），增量补充——重复检索零推理成本。
- 相似度：纯 Python cosine（几百条 × 384 维毫秒级，不引入 numpy 硬依赖）。
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import sys
import tempfile
from pathlib import Path

# 语义通路生效阈值（低于视为无关）：bge 系中文模型相关对通常 0.4+，
# 0.30 保守取低——宁可多召回交由排序打分，不制造假阴性
EMBED_MIN_SCORE = 0.30


def _warn_once(msg: str) -> None:
    global _WARNED
    if not _WARNED:
        _WARNED = True
        print(msg, file=sys.stderr)


_EMBEDDER = None  # 提供者（注入或懒加载）
_LOADED = False  # 懒解析标记
_WARNED = False
_cache: dict[str, list[float]] | None = None


def set_embedder(fn) -> None:
    """显式注入嵌入提供者：fn(texts: list[str]) -> list[list[float]]。"""
    global _EMBEDDER, _LOADED
    _EMBEDDER = fn
    _LOADED = True


def reset_for_tests() -> None:
    """重置懒解析与内存缓存（仅测试用：模拟进程重启）。"""
    global _EMBEDDER, _LOADED, _WARNED, _cache
    _EMBEDDER = None
    _LOADED = False
    _WARNED = False
    _cache = None


def _load_fastembed():
    """fastembed（可选依赖）→ 批量嵌入函数；不可用返回 None（一次告警）。"""
    try:
        from fastembed import TextEmbedding

        model = TextEmbedding(
            model_name=os.environ.get("SECPT_EMBED_MODEL", "BAAI/bge-small-zh-v1.5")
        )
    except Exception as e:
        _warn_once(f"[embed] fastembed 不可用（降级字面检索；pip install 'secpt[embed]'）: {e}")
        return None

    def _fn(texts: list[str]) -> list[list[float]]:
        return [[float(x) for x in v] for v in model.embed(texts)]

    return _fn


def get_embedder():
    """解析提供者：注入 > fastembed > None（SECPT_EMBED=0 直接 None）。只解析一次。"""
    global _EMBEDDER, _LOADED
    if _LOADED:
        return _EMBEDDER
    _LOADED = True
    if os.environ.get("SECPT_EMBED", "1") == "0":
        return None
    _EMBEDDER = _load_fastembed()
    return _EMBEDDER


def _cache_path() -> Path:
    # 调用时解析 SECPT_VAR（模块级快照会漂移——与 learning/store 同口径）
    return Path(os.environ.get("SECPT_VAR", "var")) / "knowledge" / "embed-cache.json"


def _load_cache() -> dict[str, list[float]]:
    global _cache
    if _cache is not None:
        return _cache
    _cache = {}
    p = _cache_path()
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                _cache = {
                    str(k): [float(x) for x in v] for k, v in data.items() if isinstance(v, list)
                }
        except Exception:
            _cache = {}  # 损坏缓存丢弃重建（best-effort）
    return _cache


def _save_cache(cache: dict[str, list[float]]) -> None:
    """原子落盘（tmp+replace）；失败静默（缓存是加速层，不影响正确性）。"""
    p = _cache_path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".embed-cache-")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cache, f)
        os.replace(tmp, p)
    except Exception:
        pass


def embed_cached(texts: list[str]) -> list[list[float]] | None:
    """带缓存的批量嵌入。提供者不可用/推理失败 → None（调用方降级字面检索）。"""
    emb = get_embedder()
    if emb is None:
        return None
    if not texts:
        return []
    cache = _load_cache()
    keys = [hashlib.sha1(t.encode("utf-8")).hexdigest() for t in texts]
    missing = [(k, t) for k, t in zip(keys, texts, strict=True) if k not in cache]
    if missing:
        try:
            vecs = emb([t for _k, t in missing])
            for (k, _t), v in zip(missing, vecs, strict=True):
                cache[k] = [float(x) for x in v]
        except Exception as e:
            # 推理异常或返回数量不符（strict）统一按提供者失败降级
            _warn_once(f"[embed] 推理失败（降级字面检索）: {e}")
            return None
        _save_cache(cache)
    return [cache[k] for k in keys]


def cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度（纯 Python；零向量 → 0.0）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(x * x for x in b))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)
