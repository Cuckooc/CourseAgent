"""
模块名：app.application.ports.services

作用：
    外部服务类基础设施的抽象接口（Service Port）集中定义。依据《后端分层
    重构方案》§4.2/§5（五大解耦专题）：知识库 / 文件处理 / 智能体 / 模型
    四个边界只通过本模块接口交互；实现由 infrastructure 各适配器提供：
    - LlmGateway     ← app/infrastructure/llm/（原 model_llm/）
    - Embedder       ← app/infrastructure/embeddings/（原 embedding/）
    - VectorStore    ← app/infrastructure/vector_store/（原 service/vector_store.py，阶段5 下沉）
    - DocumentParser ← app/infrastructure/document/（原 file_analysis/）
    - FileStorage    ← storage/uploads 物理文件适配器（阶段5 建立）
    - MemoryStore    ← app/infrastructure/redis/ 或进程内存实现

    阶段2 仅建立契约：以下接口尚无正式实现类绑定，运行时调用方式不变；
    逐一落地（实现 + 注入）在阶段4/5 完成。

主要成员：LlmGateway / Embedder / VectorStore / DocumentParser / FileStorage /
MemoryStore 六个服务 Port；ParsedDocument 为 DocumentParser 的契约载体。

被谁使用：阶段4/5 起由 application/domain 各用例 import；当前阶段仅供导入校验。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class ParsedDocument:
    """文档解析结果：text 为清洗后全文，meta 携带页数/类型/分栏等解析侧信息。"""

    text: str
    meta: Dict[str, Any] = field(default_factory=dict)


class LlmGateway(ABC):
    """大模型调用网关 Port（方案 §5.4）：屏蔽供应商差异，密钥统一走 core/config。"""

    @abstractmethod
    def invoke(self, prompt: str, **kwargs: Any) -> str:
        """同步调用，返回完整文本；不可用时应抛出 LLM 不可用类异常由上层降级。"""

    @abstractmethod
    def stream(self, prompt: str, **kwargs: Any) -> Iterator[str]:
        """流式调用，逐块产出文本。"""


class Embedder(ABC):
    """文本向量化 Port（原 embedding/ 的适配目标）。"""

    @abstractmethod
    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        """批量向量化文档块。"""

    @abstractmethod
    def embed_query(self, text: str) -> List[float]:
        """向量化单条查询。"""


class VectorStore(ABC):
    """向量库存取 Port（方案 §5.1）：Chroma 为其唯一实现，可整体替换。

    where 为 metadata 过滤条件（如 {"scope": "public"}），语义随实现，
    上层只关心按 metadata 过滤的能力存在。
    """

    @abstractmethod
    def get(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按 ID 或 metadata 条件取向量记录。"""

    @abstractmethod
    def query(
        self,
        embedding: List[float],
        top_k: int,
        where: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """按向量相似度检索，返回含 ids/documents/metadatas/distances 的结果。"""

    @abstractmethod
    def add(
        self,
        ids: List[str],
        embeddings: List[List[float]],
        documents: List[str],
        metadatas: List[Dict[str, Any]],
    ) -> None:
        """批量写入向量分块。"""

    @abstractmethod
    def delete(
        self,
        ids: Optional[List[str]] = None,
        where: Optional[Dict[str, Any]] = None,
    ) -> None:
        """按 ID 或 metadata 条件删除向量分块。"""

    @abstractmethod
    def flush(self) -> None:
        """落盘/冲刷缓冲（对无缓冲实现可为 no-op）。"""


class DocumentParser(ABC):
    """文档解析 Port（方案 §5.2）：parse(pdf|txt|md) -> ParsedDocument。"""

    @abstractmethod
    def parse(self, path: str) -> ParsedDocument:
        """解析单个文件为纯文本结果；不支持的类型应抛出可识别异常。"""


class FileStorage(ABC):
    """物理文件存取 Port：上传文件的落盘/读取/删除（storage/uploads 适配目标）。"""

    @abstractmethod
    def save(self, name: str, data: bytes) -> str:
        """保存文件，返回可定位该文件的存储名/路径。"""

    @abstractmethod
    def read(self, name: str) -> bytes:
        """按存储名读取文件内容。"""

    @abstractmethod
    def delete(self, name: str) -> None:
        """按存储名删除文件（不存在时静默或抛错由实现约定）。"""

    @abstractmethod
    def exists(self, name: str) -> bool:
        """按存储名判断文件是否存在。"""


class MemoryStore(ABC):
    """记忆键值存取 Port：短期/上下文记忆等带 TTL 的键值读写。"""

    @abstractmethod
    def get(self, key: str) -> Optional[str]:
        """读键，不存在或已过期返回 None。"""

    @abstractmethod
    def set(self, key: str, value: str, ttl_seconds: Optional[int] = None) -> None:
        """写键，可附带过期时间（秒）。"""

    @abstractmethod
    def delete(self, key: str) -> None:
        """删键（不存在时静默）。"""
