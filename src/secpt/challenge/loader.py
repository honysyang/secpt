"""ChallengeLoader：自动加载题目目录。

目录结构规范：
challenges/
  <challenge_id>/
    test.md          # 必需，题目描述
    attachments/     # 可选，附件目录
    tools/           # 可选，题目专属工具（ToolForge 生成）
    report.json      # 运行后生成，评测报告
    run.log          # 运行后生成，结构化日志
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class Challenge:
    """加载后的题目。"""

    id: str
    path: Path
    title: str
    description: str
    targets: list[str] = field(default_factory=list)
    attachments: list[Path] = field(default_factory=list)
    tools_dir: Path | None = None
    metadata: dict = field(default_factory=dict)

    def to_genome_objective(self) -> str:
        """转换为 Genome objective。"""
        return f"题目：{self.title}\n\n{self.description}"

    def extract_targets(self) -> list[str]:
        """从描述中提取 IP:PORT 或 URL（去重后）。"""
        text = self.description
        raw: list[str] = []

        # 完整 URL
        for m in re.finditer(r'https?://[^\s,;]+', text):
            raw.append(m.group(0))

        # IP:PORT（标准格式）
        for m in re.finditer(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}):(\d{1,5})\b', text):
            raw.append(f"{m.group(1)}:{m.group(2)}")

        # "http IP PORT" 格式（无 :// 分隔符）
        for m in re.finditer(r'http\s+(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{1,5})', text):
            raw.append(f"http://{m.group(1)}:{m.group(2)}")

        # "IP PORT" 格式
        for m in re.finditer(r'\b(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\s+(\d{3,5})\b', text):
            ip, port = m.group(1), m.group(2)
            raw.append(f"{ip}:{port}")

        # 内网地址 + 端口
        for m in re.finditer(r'内网地址\s*[:：]\s*(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})', text):
            ip = m.group(1)
            rest = text[m.end():]
            for pm in re.finditer(rf'{re.escape(ip)}\s+(\d+)', rest):
                raw.append(f"{ip}:{pm.group(1)}")

        # 归一化去重：http://host:port 与 host:port 视为同一目标
        seen: dict[str, str] = {}   # normalized → 首选形式
        result: list[str] = []
        for t in raw:
            norm = t
            if t.startswith("http://"):
                norm = t[len("http://"):]
            elif t.startswith("https://"):
                norm = t[len("https://"):]
            # 去掉尾部斜杠
            norm = norm.rstrip("/")
            if norm not in seen:
                seen[norm] = t
                result.append(t)
            # 已存在时，如果新的是 http:// 形式且旧的不是，替换
            elif t.startswith("http://") and not seen[norm].startswith("http"):
                idx = result.index(seen[norm])
                result[idx] = t
                seen[norm] = t

        return result


class ChallengeLoader:
    """题目加载器。"""

    def __init__(self, base_dir: Path | str = "challenges") -> None:
        self._base = Path(base_dir)

    def load(self, challenge_id: str) -> Challenge:
        """加载指定题目。"""
        path = self._base / challenge_id
        if not path.exists():
            raise FileNotFoundError(f"题目目录不存在: {path}")

        # 读 test.md
        test_md = path / "test.md"
        if not test_md.exists():
            raise FileNotFoundError(f"缺少 test.md: {test_md}")

        content = test_md.read_text(encoding="utf-8", errors="replace")

        # 提取标题（第一行）
        title = ""
        for line in content.split("\n"):
            line = line.strip()
            if line and not line.startswith("#"):
                title = line[:100]
                break
            if line.startswith("# "):
                title = line[2:].strip()
                break
        if not title:
            title = challenge_id

        # 附件：依次尝试常见目录名（含中文）
        attachments = []
        attachments_dir = None
        for dirname in ("附件", "attachments", "files", "assets"):
            candidate = path / dirname
            if candidate.exists() and candidate.is_dir():
                attachments_dir = candidate
                break

        if attachments_dir:
            for f in sorted(attachments_dir.iterdir()):
                if f.is_file():
                    attachments.append(f)
            # 打印文件清单
            if attachments:
                logger.info(
                    "challenge.attachments_found",
                    id=challenge_id,
                    dir=attachments_dir.name,
                    count=len(attachments),
                    files=[
                        {"name": f.name, "size": f.stat().st_size}
                        for f in attachments
                    ],
                )
            else:
                logger.info(
                    "challenge.attachments_empty",
                    id=challenge_id,
                    dir=attachments_dir.name,
                )
        else:
            logger.info("challenge.attachments_dir_missing", id=challenge_id)

        # 题目专属工具目录
        tools_dir = path / "tools"

        challenge = Challenge(
            id=challenge_id,
            path=path,
            title=title,
            description=content,
            attachments=attachments,
            tools_dir=tools_dir if tools_dir.exists() else None,
        )
        challenge.targets = challenge.extract_targets()

        logger.info(
            "challenge.loaded",
            id=challenge_id,
            title=title,
            targets=challenge.targets,
            attachments=len(attachments),
        )
        return challenge

    def list_all(self) -> list[str]:
        """列出所有题目 ID。"""
        if not self._base.exists():
            return []
        return sorted(
            d.name for d in self._base.iterdir()
            if d.is_dir() and (d / "test.md").exists()
        )

    def scaffold(self, challenge_id: str) -> Path:
        """创建题目目录骨架。"""
        path = self._base / challenge_id
        (path / "attachments").mkdir(parents=True, exist_ok=True)
        (path / "tools").mkdir(exist_ok=True)

        test_md = path / "test.md"
        if not test_md.exists():
            test_md.write_text(
                f"# {challenge_id}\n\n题目描述待填写...\n\n目标：\n",
                encoding="utf-8",
            )

        logger.info("challenge.scaffolded", id=challenge_id, path=str(path))
        return path


__all__ = ["Challenge", "ChallengeLoader"]
