"""
模块名：app.application.ports.repositories

作用：
    数据访问抽象接口（Repository Port）集中定义。依据《后端分层重构方案》
    §4.2/§4.3：业务层（application）只 import 本模块的接口，具体实现由
    app/infrastructure/persistence/repositories/（原 dao/）改写为 Adapter 后提供，
    并在组合根（app/api/deps.py，阶段5 建立）完成注入。

    权限不渗入数据层：接口签名不接收 is_admin 之类的角色判定参数，
    资源级可见性由 domain 策略 + application 用例组合完成（方案 §4.3）。

    阶段2 仅建立契约：以下接口尚无正式实现类绑定，运行时调用方式不变；
    方法集为最小集，将在阶段4/5 适配实现时按需扩充（避免过度抽象）。

主要成员：
    - UserRepository / SessionRepository / KnowledgeRepository /
      FileRepository / FeedbackRepository：五个仓储 Port；
    - DocumentMeta / DeleteResult：KnowledgeRepository 契约使用的数据载体
      （领域实体 app/domain/entities 建立后，阶段4 可再收敛替换）。

被谁使用：阶段4/5 起由 application 各用例 import；当前阶段仅供导入校验。
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DocumentMeta:
    """知识库文档元数据（契约载体，对应 Chroma metadata + 物理文件信息的投影）。

    scope 取值：public / private / temp；temp 文档以 session_id 归属会话，
    private 文档以 user_id 归属用户（可见性判定不在本层，见方案 §4.3）。
    """

    filename: str
    scope: str
    user_id: Optional[int] = None
    session_id: Optional[str] = None
    size: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)


@dataclass
class DeleteResult:
    """删除操作结果：success 为总成败，detail 供上层透传原因/统计。"""

    success: bool
    detail: str = ""


class UserRepository(ABC):
    """用户数据访问 Port（现 dao/user.py + dao/read.py 用户侧的适配目标）。"""

    @abstractmethod
    def get_by_id(self, user_id: int) -> Optional[Dict[str, Any]]:
        """按主键查用户，不存在返回 None。"""

    @abstractmethod
    def get_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        """按用户名查用户（登录/注册唯一性校验用），不存在返回 None。"""

    @abstractmethod
    def create(self, user: Dict[str, Any]) -> int:
        """创建用户，返回新用户 ID。"""

    @abstractmethod
    def update(self, user_id: int, fields: Dict[str, Any]) -> bool:
        """更新用户字段，返回是否命中记录。"""


class SessionRepository(ABC):
    """会话数据访问 Port（现 dao/session.py 的适配目标）。"""

    @abstractmethod
    def create(self, user_id: int, title: str = "") -> str:
        """为用户创建会话，返回会话 ID。"""

    @abstractmethod
    def get(self, session_id: str) -> Optional[Dict[str, Any]]:
        """按会话 ID 查会话，不存在返回 None。"""

    @abstractmethod
    def list_by_user(self, user_id: int) -> List[Dict[str, Any]]:
        """列出用户全部会话（按更新时间倒序）。"""

    @abstractmethod
    def delete(self, session_id: str) -> bool:
        """删除会话，返回是否命中记录。"""


class KnowledgeRepository(ABC):
    """知识库文档访问 Port（现 dao/knowledge.py 的适配目标，方案 §4.2 示例接口）。

    只做 CRUD：可见性过滤由 application 用例组合 domain 策略完成，
    本接口不出现 user 视角之外的权限参数。
    """

    @abstractmethod
    def list_all(self) -> List[DocumentMeta]:
        """列出全部文档元数据（不过滤可见性）。"""

    @abstractmethod
    def get(self, filename: str) -> Optional[DocumentMeta]:
        """按存储文件名查单文档，不存在返回 None。"""

    @abstractmethod
    def delete(self, filename: str) -> DeleteResult:
        """删除文档（向量分块 + 物理文件），返回删除结果。"""


class FileRepository(ABC):
    """文件解析/审核记录访问 Port（现 dao/document_review.py 的适配目标）。"""

    @abstractmethod
    def get(self, review_id: int) -> Optional[Dict[str, Any]]:
        """按主键查审核记录，不存在返回 None。"""

    @abstractmethod
    def list_pending(self) -> List[Dict[str, Any]]:
        """列出待审核记录。"""

    @abstractmethod
    def update_status(self, review_id: int, status: str) -> bool:
        """更新审核状态，返回是否命中记录。"""


class FeedbackRepository(ABC):
    """对话反馈访问 Port（现 dao/feedback.py 的适配目标）。"""

    @abstractmethod
    def add(self, feedback: Dict[str, Any]) -> int:
        """新增反馈记录，返回记录 ID。"""

    @abstractmethod
    def list_by_session(self, session_id: str) -> List[Dict[str, Any]]:
        """列出某会话的反馈记录。"""
