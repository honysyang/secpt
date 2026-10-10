"""证据层：内容寻址快照与 findings 绑定（Phase B3）。

EvidenceStore 按 SHA-256 内容寻址存 blob（天然去重），索引 SQLite 记录
finding -> blob 绑定（baseline/proof/verification/supporting 四角色），
支持把 traffic 层的 exchange 快照成标准信封并导出为
request.http / response.http 风格文件 + manifest.json。
"""

from secpt.evidence.store import VALID_ROLES, EvidenceStore, serialize_exchange

__all__ = ["VALID_ROLES", "EvidenceStore", "serialize_exchange"]
