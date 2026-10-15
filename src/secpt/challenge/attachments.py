"""附件处理器：解包 zip 等附件。

文件名净化（跨平台）：附件名/压缩包内 entry 名常带平台查询串
（如 "b.zip?attname=real.zip"），而 Windows 文件系统禁止 "? * < > | \" 等字形，
落盘即 OSError。统一在 extract 入口逐段净化（剥查询串 + 替换禁止字形）。
"""

from __future__ import annotations

import shutil
import tarfile
import zipfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)

# Windows 文件名禁止字形（? 已随查询串剥离，此处兜底其余）+ 控制字符
_WIN_FORBIDDEN = '<>:"|*'


def sanitize_filename(name: str) -> str:
    """单文件名/目录段净化：剥 '?' 起的查询串、替换 Windows 禁止字形。

    "b.zip?attname=real.zip" → "b.zip"（保留真实扩展名）；
    "file<1>.txt" → "file_1_.txt"；剥尾点/空格（Windows 不允许）；空名 → "attachment"。
    """
    name = name.split("?", 1)[0]  # 查询串及之后全部剥离
    name = "".join("_" if (ch in _WIN_FORBIDDEN or ord(ch) < 32) else ch for ch in name)
    name = name.rstrip(" .")
    return name or "attachment"


def _safe_rel_parts(entry_name: str) -> list[str]:
    """entry 名 → 净化后的相对路径段（逐段 sanitize，滤穿越段）。

    统一按 / 与 \\ 分段；绝对路径前导段与 ".." 直接丢弃（防穿越）。
    """
    parts = []
    for seg in entry_name.replace("\\", "/").split("/"):
        if seg in ("", ".", ".."):
            continue  # 空段/当前段/穿越段
        parts.append(sanitize_filename(seg))
    return parts


class AttachmentHandler:
    """处理题目附件。"""

    def __init__(self, extract_dir: Path) -> None:
        self._extract_dir = extract_dir
        self._extract_dir.mkdir(parents=True, exist_ok=True)

    def extract(self, file_path: Path) -> list[Path]:
        """解包附件，返回解包后的文件列表。"""
        suffix = file_path.suffix.lower()

        if suffix == ".zip":
            return self._extract_zip(file_path)
        elif suffix in (".tar", ".gz", ".tgz", ".tar.gz"):
            return self._extract_tar(file_path)
        else:
            # 非压缩文件，直接返回
            return [file_path]

    def _target_under(self, extract_to: Path, parts: list[str]) -> Path | None:
        """净化段 → 目标路径；穿越校验不过返回 None。"""
        target = (extract_to / Path(*parts)).resolve()
        if not str(target).startswith(str(extract_to.resolve())):
            return None
        return target

    def _extract_zip(self, zip_path: Path) -> list[Path]:
        """解包 zip（逐条目净化提取：entry 名剥查询串 + 替换 Windows 禁止字形）。"""
        extracted = []
        extract_to = self._extract_dir / sanitize_filename(zip_path.stem)
        extract_to.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                for info in zf.infolist():
                    parts = _safe_rel_parts(info.filename)
                    if not parts:
                        continue
                    target = self._target_under(extract_to, parts)
                    if target is None:
                        logger.warning("attachment.path_traversal", name=info.filename)
                        continue
                    if info.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    extracted.append(target)
        except zipfile.BadZipFile as e:
            logger.error("attachment.bad_zip", path=str(zip_path), error=str(e))
            return [zip_path]

        logger.info(
            "attachment.zip_extracted",
            path=str(zip_path),
            count=len(extracted),
            to=str(extract_to),
        )
        return extracted

    def _extract_tar(self, tar_path: Path) -> list[Path]:
        """解包 tar（逐成员净化提取，与 zip 同净化规则）。"""
        extracted = []
        extract_to = self._extract_dir / sanitize_filename(tar_path.stem)
        extract_to.mkdir(parents=True, exist_ok=True)

        try:
            with tarfile.open(tar_path, "r:*") as tf:
                for member in tf.getmembers():
                    parts = _safe_rel_parts(member.name)
                    if not parts:
                        continue
                    target = self._target_under(extract_to, parts)
                    if target is None:
                        logger.warning("attachment.path_traversal", name=member.name)
                        continue
                    if not member.isfile():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tf.extractfile(member) as src, open(target, "wb") as dst:
                        if src is not None:
                            shutil.copyfileobj(src, dst)
                    extracted.append(target)
        except tarfile.TarError as e:
            logger.error("attachment.bad_tar", path=str(tar_path), error=str(e))
            return [tar_path]

        logger.info(
            "attachment.tar_extracted",
            path=str(tar_path),
            count=len(extracted),
        )
        return extracted


__all__ = ["AttachmentHandler", "sanitize_filename"]
