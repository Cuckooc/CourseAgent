"""
模块名：app.domain.tools.business.knowledge_business

作用：
    知识库检索类工具的业务函数所在地（function calling 工具层的业务层）。
    模块在导入时通过两次 register_tool(ToolSpec(...)) 自注册两个工具，
    分别归属两个决策层，数据域严格隔离：
- knowledge_search_fn      → knowledge_search 工具，RAGAgent 域：
                             公共库 + 本人私有库（持久库）；
- session_file_search_fn   → session_file_search 工具，FileAgent 域：
                             仅当前会话临时库（{uid}_{sid} 物理库）。

本模块为纯业务实现：不含任何"要不要检索"的决策，不 import 任何 Agent，
可被 ToolDispatcher、未来的 HTTP 接口、单元测试直接调用。
检索算法原样复用 app.domain.agents.retrieval.retrieve_scoped
（向量+关键词混合检索 → RRF 融合 → gte-rerank 精排，Small-to-Big 父块取回）。

主要成员：
    - knowledge_search_fn / session_file_search_fn：两个 BusinessFn 业务函数；
    - _doc_to_dto：LangChain Document → 旧契约 dict 的转换器；
    - 模块尾部两条 register_tool(...)：工具自注册声明（名称/描述/入参模型/
      归属决策层/风险级别/超时/降级均取 ToolSpec 默认值，即 T0 只读、
      超时 settings.TOOL_DEFAULT_TIMEOUT_SECONDS、失败可降级空结果）。

被谁使用（间接调用链）：
    - app/domain/tools/registry.py 首次查询时 import app.domain.tools.business → 本模块完成注册；
    - app/domain/tools/dispatcher.py 的 ToolDispatcher._run_one() 经 ToolSpec.business_fn
      回调这两个函数（上游入口为 StructuredTool 直调或 app/domain/tools/shadow.py）；
    - tests/app/domain/tools/test_tool_layer_p1.py 直接构造参数模型与 ToolContext 调用；
    - 数据源依赖：app/infrastructure/vector_store/persistent.get_persistent_db（持久 Chroma 库）、
      app/infrastructure/vector_store/temp_store.get_temp_store（会话临时库注册表）、
      app/domain/agents/retrieval.retrieve_scoped（检索算法）。
"""
from __future__ import annotations

import logging

from langchain_core.documents import Document

from app.domain.agents.retrieval import retrieve_scoped
from app.application.ports.vector import get_temp_store
from app.application.ports.vector import get_persistent_db
from app.domain.tools.protocol import (
    KnowledgeSearchArgs,
    SessionFileSearchArgs,
    ToolContext,
    ToolResult,
    ToolSpec,
)
from app.domain.tools.registry import register_tool

logger = logging.getLogger(__name__)


def _doc_to_dto(doc: Document, prefer_output: bool) -> dict:
    """把 LangChain Document 转换为与 MessageBus 旧契约一致的 DTO：{content, metadata}。

    被谁调用：knowledge_search_fn（prefer_output=True）与
              session_file_search_fn（prefer_output=False）逐条转换召回结果，
              以保持与 rag_agent/file_agent 旧链路发布给 SummaryAgent 的
              消息结构完全一致，影子模式指纹对比与下游消费均可复用。
    参数：
        doc：retrieve_scoped 返回的 LangChain Document（page_content + metadata）；
        prefer_output：正文取值策略——持久库内置 JSON 数据的正文存
                       metadata.output，故优先取 output；临时库/父子块正文
                       在 page_content，直接取 page_content。
    返回：{"content": 正文 str, "metadata": 文档元数据 dict}；
          metadata 缺失时按空 dict 处理，output 缺失时回退 page_content。
    """
    meta = doc.metadata or {}
    # prefer_output=True：先取内置 JSON 的 output 字段，缺省回退父块正文
    content = (meta.get("output") or doc.page_content) if prefer_output else doc.page_content
    return {"content": content, "metadata": meta}


# ──────────────────────────────────────────────────────────────
# RAGAgent 域：公共 + 本人私有（持久库）
# ──────────────────────────────────────────────────────────────
def knowledge_search_fn(
    args: KnowledgeSearchArgs, ctx: ToolContext
) -> ToolResult:
    """工具 knowledge_search 的业务函数：检索持久知识库（公共库 + 本人私有库）。

    功能：按用户问题在持久化向量库做混合检索，数据域 scope（public + 本人
          private）由 user_id 在检索层（retrieve_scoped → build_scope_filter）
          强制过滤，LLM 无法越权访问他人私有库。
    被谁调用：不由外部直接 import 调用；注册为 ToolSpec.business_fn 后，
              由 app/domain/tools/dispatcher.py 的 ToolDispatcher._run_one() 在线程池中
              回调（影子模式经 app/domain/tools/shadow.py，未来主链路经 Agent function
              calling）；tests/app/domain/tools/test_tool_layer_p1.py 单测直调。
    工具名/入参 schema：knowledge_search；入参模型 KnowledgeSearchArgs
              （{"query": str, "top_k": int=3}，dispatcher 已完成 query 截断
              200 字、top_k 钳制 [1,10]）。
    参数：args 为校验后的入参模型（query 来源 LLM 决策的检索词，top_k 召回条数）；
          ctx 为服务端注入上下文（user_id 是 scope 过滤的唯一身份来源）。
    数据源：app/infrastructure/vector_store/persistent.get_persistent_db() 返回的持久 Chroma 库；
            检索算法 app/domain/agents/retrieval.retrieve_scoped。
    返回：ToolResult，data 为 [{"content", "metadata"}, ...]（content 优先取
          metadata.output），去向为回灌 Agent 拼入 LLM prompt / SSE 答案，
          影子模式下供 app/domain/tools/shadow.py 做指纹对比；query 为空时返回成功空集。
    显式不传 session_id：retrieve_scoped 仅在 session_id 非空时合并临时库，
          临时库归属 FileAgent 域，避免同一片段被两路重复召回。
    """
    # 空查询不触达向量库，直接返回成功空集（dispatcher 已做过一轮空值钳制）
    if not args.query:
        return ToolResult.empty("knowledge_search")

    # 持久库单例（公共 + 私有 scope 的数据域过滤在 retrieve_scoped 内完成）
    db = get_persistent_db()
    docs = retrieve_scoped(
        db,
        args.query,
        top_k=args.top_k,
        user_id=ctx.user_id,   # 身份只取自服务端 ctx，禁止 LLM 参数注入
        session_id=None,       # 不合并会话临时库（临时库归 FileAgent 域）
    )
    return ToolResult(
        name="knowledge_search",
        success=True,
        # 持久库内置 JSON 数据正文在 metadata.output，故 prefer_output=True
        data=[_doc_to_dto(d, prefer_output=True) for d in docs],
    )


# ──────────────────────────────────────────────────────────────
# FileAgent 域：当前会话临时库
# ──────────────────────────────────────────────────────────────
def session_file_search_fn(
    args: SessionFileSearchArgs, ctx: ToolContext
) -> ToolResult:
    """工具 session_file_search 的业务函数：检索当前会话上传文件形成的临时知识库。

    功能：在 {user_id}_{session_id} 物理隔离的会话临时向量库中检索本会话
          上传文件切片；只认当前会话，不跨会话、不触达他人临时库。
    被谁调用：注册为 ToolSpec.business_fn 后由
              app/domain/tools/dispatcher.py 的 ToolDispatcher._run_one() 回调
              （影子模式经 app/domain/tools/shadow.py）；tests/tools 单测直调。
    工具名/入参 schema：session_file_search；入参模型 SessionFileSearchArgs
              （{"query": str, "top_k": int=3}，同样经 dispatcher 钳制）。
    参数：args.query 来源 LLM 决策的检索词；ctx.user_id/ctx.session_id 为
          服务端注入的临时库定位键（LLM 参数中伪造一律无效）。
    数据源：app/infrastructure/vector_store/temp_store.get_temp_store() 的会话临时库注册表
            （has_session 判断物理库是否存在、retrieve_scoped 内部 get_db 加载），
            检索算法仍为 app/domain/agents/retrieval.retrieve_scoped。
    返回：ToolResult，data 为 [{"content", "metadata"}, ...]（正文取
          page_content，prefer_output=False）；以下三种情形返回成功空集：
          ①query 为空；②ctx 缺 user_id/session_id；③该会话无临时库。
    """
    if not args.query:
        return ToolResult.empty("session_file_search")
    # 无身份/会话无法定位临时库 → 空集（非降级，属正常无数据）
    if not (ctx.user_id and ctx.session_id):
        return ToolResult.empty("session_file_search")

    # 先经临时库注册表确认本会话物理库存在，避免 retrieve_scoped 内无谓加载
    store = get_temp_store()
    if not store.has_session(ctx.user_id, ctx.session_id):
        return ToolResult.empty("session_file_search")

    # db 必须传 None：retrieve_scoped 的 db 形参语义是"持久库"，临时库由
    # session_id 旁路加载（内部再 get_db 与传入 db 做同一性判断，同实例则跳过）。
    # 若把 temp_db 传给 db，临时库旁路会被跳过，而持久库路径的 scope 过滤
    # （public/private）会把 scope=temp 数据全部滤除，导致召回恒为 0。
    docs = retrieve_scoped(
        None,                  # 不传持久库实例，强制走 session_id 临时库旁路
        args.query,
        top_k=args.top_k,
        user_id=ctx.user_id,
        session_id=ctx.session_id,
    )
    return ToolResult(
        name="session_file_search",
        success=True,
        # 临时库父子块正文在 page_content，故 prefer_output=False
        data=[_doc_to_dto(d, prefer_output=False) for d in docs],
    )


# ──────────────────────────────────────────────────────────────
# 自注册（import app.domain.tools.business 时生效）
# ──────────────────────────────────────────────────────────────
# —— 工具 1：knowledge_search（风险/超时/角色/降级均取 ToolSpec 默认值：
#    T0 只读、默认角色 user/teacher/admin、超时 TOOL_DEFAULT_TIMEOUT_SECONDS、失败可降级空集）
register_tool(ToolSpec(
    name="knowledge_search",          # LLM function calling 决策出的 tool_name 须精确匹配
    # description 原样进入 bind_tools 的 JSON Schema，指导 LLM 何时选择本工具
    description=(
        "检索课程咨询公共知识库与用户个人知识库。"
        "当用户的问题涉及课程、学习方法、考试规划、知识概念等需要权威资料支撑时调用。"
        "参数 query 为经过理解改写的检索词，top_k 为需要召回的条数（1-10）。"
    ),
    args_model=KnowledgeSearchArgs,   # 入参 schema 来源（dispatcher 校验/钳制同一模型）
    business_fn=knowledge_search_fn,  # 执行体：持久库（公共 + 本人私有）检索
    owner_agent="RAGAgent",           # 归属决策层：dispatcher 跨域防护的依据
))

# —— 工具 2：session_file_search（默认值同上；数据域为当前会话临时库）
register_tool(ToolSpec(
    name="session_file_search",
    # description 原样进入 bind_tools 的 JSON Schema
    description=(
        "检索用户在【当前会话】中上传的文件内容（会话临时知识库）。"
        "当用户提到'我上传的资料/文档/文件'或问题明显围绕本次上传材料时调用。"
        "参数 query 为检索词，top_k 为召回条数（1-10）。"
    ),
    args_model=SessionFileSearchArgs,
    business_fn=session_file_search_fn,  # 执行体：{uid}_{sid} 会话临时库检索
    owner_agent="FileAgent",
))
