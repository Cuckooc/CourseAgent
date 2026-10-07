"""
模块名：service.knowledge_service
作用：知识库管理服务（应用级单例），是知识库列表/详情/删除接口与
      dao/knowledge.py 的 KnowledgeDAO 之间的薄业务层。协调向量库记录
      （chromadb_data 中的父子块）与上传物理文件（UPLOAD_DIR）的生命周期，
      支持按用户范围（公共/私有/临时）过滤与权限校验，删除成功时记录审计日志。

主要成员：
- KnowledgeService：知识库管理服务类。
- KnowledgeService.list_documents()：列出当前用户可见的知识库文档。
- KnowledgeService.get_document_info()：查询单个文档详情（下载前校验）。
- KnowledgeService.delete_document()：删除文档向量块与物理文件。
- get_knowledge_service()：lru_cache 单例工厂。

被谁使用：
- control/knowledge_control.py：以 get_knowledge_service() 单例方式调用，
  分别服务知识库列表、文档详情/下载、文档删除三个接口；user_id 来自
  JWT，is_admin 由 control 层按角色判定，结果返回知识库管理前端。
"""
import logging
from functools import lru_cache
from typing import Any, Dict, List, Optional

from core.config import settings
from app.infrastructure.persistence.repositories.knowledge import KnowledgeDAO

# 模块级日志器：文档删除等管理动作的审计日志走该 logger
logger = logging.getLogger(__name__)


class KnowledgeService:
    """知识库文档管理服务（列表/详情/删除的权限感知业务层）。

    类作用：把 control 层的用户身份（user_id/is_admin）透传给 KnowledgeDAO，
            由 DAO 完成向量库按 scope 过滤检索、归属权限校验、向量块删除与
            物理文件清理；本类只负责单例持有 DAO 与删除审计日志。

    实例化位置：不在 control 层直接 new；唯一创建处为本模块末尾的
            get_knowledge_service()（@lru_cache 应用级单例），由
            control/knowledge_control.py 的三个接口调用。

    关键 self 属性：
    - dao：dao/knowledge.py 的 KnowledgeDAO 实例，构造参数为
      settings.UPLOAD_DIR（物理文件根目录）；本类全部方法都委托它读写
      chroma 向量库与上传目录。
    """

    def __init__(self):
        # __init__ 无形参：上传根目录取自 core.config.settings.UPLOAD_DIR
        # 数据去向：KnowledgeDAO 据此定位并清理上传的物理文件
        self.dao = KnowledgeDAO(settings.UPLOAD_DIR)

    def list_documents(self, user_id: int = None, is_admin: bool = False) -> List[Dict[str, Any]]:
        """列出当前用户可见的知识库文档。

        功能：委托 DAO 按 scope/user_id 过滤文档（普通用户仅见公共库 +
              自有私有库，管理员可见范围由 DAO 按 is_admin 放宽）。
        被谁调用：control/knowledge_control.py 的知识库列表接口。
        参数：
        - user_id (int|None)：当前用户 ID，来源：JWT current_user。
        - is_admin (bool)：是否管理员视角，来源：control 层按角色判定。
        返回：List[Dict[str, Any]]——文档信息列表（数据来源：KnowledgeDAO
              扫描 chroma 父块元数据）；去向：列表接口 JSON 返回前端。
        """
        return self.dao.list_documents(user_id=user_id, is_admin=is_admin)

    def get_document_info(self, filename: str, user_id: int = None, is_admin: bool = False) -> Optional[Dict[str, Any]]:
        """查询单个文档详情（含下载/预览前的权限校验）。

        功能：委托 DAO 按存储定位文档并校验当前用户是否有权访问。
        被谁调用：control/knowledge_control.py 的文档详情接口。
        参数：
        - filename (str)：存储文件名，来源：请求体 req.stored_name。
        - user_id (int|None)：JWT 注入的当前用户 ID。
        - is_admin (bool)：control 层按角色判定的管理员标记。
        返回：Optional[Dict]——文档元数据（命中且有权限）；None 表示
              不存在或无权访问（control 层据此返回 404/403 语义）。
        """
        return self.dao.get_document_info(filename, user_id=user_id, is_admin=is_admin)

    def delete_document(self, filename: str, user_id: int = None, is_admin: bool = False) -> Optional[Dict[str, Any]]:
        """删除知识库文档：向量块 + 物理文件一并清理。

        功能：委托 DAO 删除该文件的全部父子向量块并删除 UPLOAD_DIR 下
              物理文件（DAO 内部先做归属/权限校验），删除成功后写审计日志。
        被谁调用：control/knowledge_control.py 的文档删除接口。
        参数：
        - filename (str)：存储文件名，来源：请求体 req.stored_name。
        - user_id (int|None)：JWT 注入的当前用户 ID。
        - is_admin (bool)：control 层按角色判定的管理员标记。
        返回：Optional[Dict]——成功时为删除结果（含 deleted_chunks 删除
              块数、file_removed 物理文件是否删除）；None 表示文档不存在
              或无权删除。去向：删除接口 JSON 返回前端。
        """
        result = self.dao.delete_document(filename, user_id=user_id, is_admin=is_admin)
        if result is not None:
            # 审计日志：记录文档名、删除的向量块数与物理文件删除结果
            logger.info(
                "knowledge document deleted: %s chunks=%s file=%s",
                filename,
                result["deleted_chunks"],
                result["file_removed"],
            )
        return result


@lru_cache(maxsize=1)
def get_knowledge_service() -> KnowledgeService:
    """KnowledgeService 应用级单例工厂。

    被谁调用：control/knowledge_control.py 的列表/详情/删除接口。
    返回：KnowledgeService 唯一实例（内部 KnowledgeDAO 随之只构建一次）。
    """
    return KnowledgeService()
