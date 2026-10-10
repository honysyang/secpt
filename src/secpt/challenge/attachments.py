"""附件处理器：解包 zip 等附件。"""

from __future__ import annotations

import zipfile
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


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

    def _extract_zip(self, zip_path: Path) -> list[Path]:
        """解包 zip。"""
        extracted = []
        extract_to = self._extract_dir / zip_path.stem
        extract_to.mkdir(parents=True, exist_ok=True)

        try:
            with zipfile.ZipFile(zip_path, "r") as zf:
                # 安全检查：防止路径穿越
                for name in zf.namelist():
                    target = (extract_to / name).resolve()
                    if not str(target).startswith(str(extract_to.resolve())):
                        logger.warning("attachment.path_traversal", name=name)
                        continue
                zf.extractall(extract_to)
                extracted = list(extract_to.rglob("*"))
                extracted = [f for f in extracted if f.is_file()]
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
        """解包 tar。"""
        import tarfile

        extracted = []
        extract_to = self._extract_dir / tar_path.stem
        extract_to.mkdir(parents=True, exist_ok=True)

        try:
            with tarfile.open(tar_path, "r:*") as tf:
                # 安全检查
                for member in tf.getmembers():
                    target = (extract_to / member.name).resolve()
                    if not str(target).startswith(str(extract_to.resolve())):
                        logger.warning("attachment.path_traversal", name=member.name)
                        continue
                tf.extractall(extract_to)
                extracted = list(extract_to.rglob("*"))
                extracted = [f for f in extracted if f.is_file()]
        except tarfile.TarError as e:
            logger.error("attachment.bad_tar", path=str(tar_path), error=str(e))
            return [tar_path]

        logger.info(
            "attachment.tar_extracted",
            path=str(tar_path),
            count=len(extracted),
        )
        return extracted


__all__ = ["AttachmentHandler"]
