"""
模块名：app.infrastructure.embeddings.text_embedding

作用：
    内置 JSON 问答知识库的离线入库与向量检索工具集。处理流水线为：
    加载 JSON → 转换为 LangChain Document → 递归字符分块 → 调用 DashScope
    text-embedding-v2 向量化 → 构建/加载本地持久化 Chroma 库（默认
    storage/chromadb，可由环境变量 chroma_dir 覆盖），并提供按可见范围（public/private）与 L2 距离阈值
    过滤的检索函数。

主要成员：
    - load_json_data / json_to_documents / split_documents：JSON 知识库 ETL 三步；
    - get_embedding：lru_cache 全局单例，返回 DashScopeEmbeddings 客户端；
    - build_chromadb：持久化 Chroma 库的加载（目录非空）或分批构建；
    - build_scope_filter：构造「公共 + 当前用户私有」的 Chroma where 过滤条件；
    - MAX_L2_DISTANCE：L2 距离相关性上限常量；
    - test_retrieval / test_retrieval_temp：持久化库 / 会话临时内存库检索。

被谁使用（Grep "embedding.text_embedding" 确认）：
    - app/domain/agents/rag_agent.py（JSON ETL 三步 + build_chromadb）、file_agent.py；
    - app/domain/agents/retrieval.py（get_embedding / build_scope_filter / MAX_L2_DISTANCE）；
    - app/infrastructure/vector_store/persistent.py、file_service.py、temp_knowledge_store.py（get_embedding）；
    - file_analysis/file.py、app/application/chat/context.py 及 scripts/ 下调试/维护脚本。

向量维度一致性约束：
    text-embedding-v2 固定输出 1536 维向量。同一 Chroma collection 内的存量向量与
    查询向量必须来自同一模型，维度不一致会使距离计算报错或检索静默失效，故入库后
    不得更换 embedding 模型。带失败重试的包装版见 embedding/embedding_model.py
    （同样锁定 text-embedding-v2，只重试、不换模型）。

API 密钥：
    dashscope.api_key 来自 env/qianwen_config.env 的 api_key（本地文件，禁止入库）。
"""
from __future__ import annotations

import os
import logging
import json
import dashscope
from functools import lru_cache

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_chroma import Chroma
from langchain_core.documents import Document
from langchain_community.embeddings import DashScopeEmbeddings

from core.config import settings
import config.setting  # noqa: F401  # 环境变量由 config/setting.py 导入期统一 load_dotenv(env/qianwen_config.env) 加载

# 模块导入时配置根日志：INFO 级别 + 标准时间格式，StreamHandler 强制输出到终端（不依赖外部 logging 配置）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]  # 强制输出到终端
)

logger=logging.getLogger(__name__)
# DashScope 全局 api_key：来自 qianwen_config.env 的 api_key 环境变量，供 embeddings 客户端鉴权
dashscope.api_key = os.getenv('api_key')
def load_json_data(json_file_path:str)->list[dict]:
    """读取内置 JSON 问答知识库文件并解析为 dict 列表。

    被谁调用：本模块 __main__ 离线建库流程；app/domain/agents/rag_agent.py 懒加载公共知识库。
    参数：json_file_path —— JSON 文件路径（主流程来自 data/LearnPlan_Dialogue_Collection 目录）。
    返回：list[dict]，每条含 instruction/input/output 字段，去向 json_to_documents。
    异常：文件不存在或 JSON 非法时由 open/json.load 直接抛出（离线脚本无 fallback）。
    """
    with open(json_file_path,'r',encoding='utf-8') as f:
        data=json.load(f)
    logger.info(f'load json file {json_file_path} success')
    return data
def json_to_documents(json_data:list[dict])->list[Document]:
    """把 JSON 问答记录转换为 LangChain Document 列表。

    功能：将每条记录拼成「用户问题 + 老师回答」文本作为 page_content，并把
    index/instruction/input/output 与 scope=public 写入 metadata。
    被谁调用：本模块 __main__；app/domain/agents/rag_agent.py 构建公共知识库。
    参数：json_data —— load_json_data 的解析结果（DAO/JSON 文件来源）。
    返回：list[Document]，去向 split_documents 分块后写入 Chroma。
    """
    docs=[]
    for index,item in enumerate(json_data):
        text=f"用户问题:{item.get('instruction','')}\n老师回答:{item.get('output','')}"
        metadata={
            "index":index,
            "instruction":item.get("instruction",""),
            "input":item.get("input",""),
            "output":item.get("output",""),
            "scope":"public",
        }
        docs.append(Document(page_content=text,metadata=metadata))
    logger.info(f'json_to_documents success,total {len(docs)} documents')
    return docs
def split_documents(docs:list[Document])->list[Document]:
    """递归字符分块（固定块策略，用于内置 JSON 公共知识库）。

    功能：使用 RecursiveCharacterTextSplitter 按 512 字符切块、相邻块重叠 50
    字符，add_start_index 在 metadata 中记录块内起始偏移；长度函数为字符数 len。
    被谁调用：本模块 __main__；app/domain/agents/rag_agent.py。
    参数：docs —— json_to_documents 产出的 Document 列表。
    返回：分块后的 list[Document]，去向 build_chromadb 入库。
    说明：上传文件的父子分块（parent_child）是另一套策略，不在此函数处理。
    """
    splitter=RecursiveCharacterTextSplitter(chunk_size=512,
                                            chunk_overlap=50,
                                            length_function=len,
                                            add_start_index=True)
    split_doc=splitter.split_documents(docs)
    logger.info(f'split_documents success,total {len(split_doc)} documents')
    return split_doc

    
@lru_cache(maxsize=1)
def get_embedding():
    """获取 DashScope text-embedding-v2 嵌入客户端（进程级单例）。

    功能：构造 langchain DashScopeEmbeddings；@lru_cache(maxsize=1) 保证全进程
    只实例化一次，重复调用返回同一对象。
    被谁调用：app/domain/agents/retrieval.py、rag_agent.py、app/infrastructure/vector_store/persistent.py、
    file_service.py、temp_knowledge_store.py、file_analysis/file.py、app/application/chat/context.py
    及 scripts 维护脚本，作为 Chroma 的 embedding_function。
    返回：DashScopeEmbeddings 实例（1536 维），去向 Chroma 建库/查询向量化，
    最终向量写入本地 chromadb_data 持久化目录。
    异常：本函数不发起远程调用，无网络异常；实际向量化失败由调用方处理。
    注意：app/application/chat/chat_service.py 使用的是 embedding/embedding_model.py 的同名
    函数（带重试与查询缓存的包装版），两者模型一致、维度一致。
    """
    logger.info('get_embedding start')
    embedding_model=DashScopeEmbeddings(
        model="text-embedding-v2",
        dashscope_api_key=dashscope.api_key,
    )
    return embedding_model
def build_chromadb(docs:list[Document],embedding_model,persist_path:str=None)->Chroma:
    """加载已有持久化 Chroma 库；不存在时用 docs 分批构建并持久化。

    功能：persist_path 已存在且非空 → 直接打开（不重复入库），返回库内条数；
    否则新建 Chroma 并按 batch_size=25 分批 add_documents（控制单次 API 批量）。
    被谁调用：本模块 __main__；app/domain/agents/rag_agent.py（公共库）、file_agent.py。
    参数：docs —— 分块后的 Document（仅首次建库使用）；
          embedding_model —— get_embedding() 返回的嵌入客户端；
          persist_path —— Chroma 持久化目录；None 时取 settings.CHROMA_DIR
          （默认 storage/chromadb，可用环境变量 chroma_dir 覆盖）。
    返回：langchain Chroma 实例，去向各 Agent 的 similarity_search_with_score 检索。
    异常：embedding API 超时/限流由 DashScope SDK 内部处理；批量写入中途失败时
    已成功的批次已落盘，重跑本函数会因目录非空走「直接打开」分支（不会自动补写）。
    """
    if persist_path is None:
        persist_path = str(settings.CHROMA_DIR)
    logger.info('build_chromadb start')
    if os.path.exists(persist_path) and len(os.listdir(persist_path)) > 0:
        logger.info('chromadb_data exists')
        db=Chroma(persist_directory=persist_path,
                  embedding_function=embedding_model)
        count=db._collection.count()  
        logger.info(f"load existing chromadb success,total {count} documents")
        return db
    logger.info('chromadb_data not exists')
    db=Chroma(
       
        embedding_function=embedding_model,
        persist_directory=persist_path
        
    )
    # 每批写入条数：控制单次 embedding/写入请求规模，避免大批量触发 API 限流
    batch_size=25
    total=len(docs)
    for i in range(0, total, batch_size):
        batch = docs[i:i+batch_size]
        db.add_documents(batch)
        logger.info(f"✅ 已插入 {min(i+batch_size, total)}/{total} 条向量")
    count=db._collection.count()
    logger.info(f"build_chromadb success,total {count} documents")
    return db
def build_scope_filter(user_id: int = None):
    """构造持久化库可见范围过滤（仅 scope 维度，可直接用作 Chroma where）。

    可见范围 = 公共(public) + 当前用户私有(private)。
    版本新鲜度 is_latest 不能放在 Chroma where 中：Chroma 不支持 $exists
    操作符，且 10k+ 历史块没有 is_latest 字段（放 where 会令整个 query 抛错，
    曾导致登录用户的向量/关键词检索全部静默失效）。旧版本块改由检索层在
    Python 侧过滤（is_latest is False 丢弃，字段缺失或 True 保留）。

    被谁调用：本模块 test_retrieval；app/domain/agents/retrieval.py 持久化库检索；
    scripts/dev/debug 下调试脚本与 tests/phase/test_dedup_version.py。
    参数：user_id —— 当前登录用户 ID（来源：登录态 JWT/DAO 透传）；None 表示
          未登录/公共场景，仅放行 public。
    返回：dict，Chroma where 条件（$or 组合 public 与 private+user_id），
          去向 similarity_search_with_score 的 filter 参数。
    """
    if user_id is not None:
        return {
            "$or": [
                {"scope": "public"},
                {"$and": [{"scope": "private"}, {"user_id": user_id}]},
            ]
        }
    return {"scope": "public"}


# Chroma 默认 hnsw 空间为 L2 距离（越小越相似）。
# 实测 DashScope embedding 下：强相关 0.5~1.1，无关 >=1.25，取 1.15 作为上限。
# 含义：检索得分超过该值即判为不相关丢弃；被 test_retrieval 与 app/domain/agents/retrieval.py
# （legacy 距离阈值参数）读取。
MAX_L2_DISTANCE = 1.15


def test_retrieval(db: Chroma, query: str, top_k: int = 3, user_id: int = None, session_id: int = None)->list[Document]:
    """RAG 检索（持久化库）：按知识库范围过滤 + L2 距离阈值。
    可见范围 = 公共(public) + 当前用户私有(private)。
    会话临时知识库(scope=temp)不持久化，由 service 层合并内存库结果。

    被谁调用：本模块 __main__ 的离线自检（线上检索主路径在 app/domain/agents/retrieval.py，
    复用同样的过滤与阈值策略）。
    参数：db —— build_chromadb 加载的持久化 Chroma；
          query —— 用户问题文本（经上下文改写后传入 multi_agent）；
          top_k —— 最多返回块数；user_id/session_id —— 登录用户与会话 ID（用于范围过滤）。
    返回：list[Document] 相关上下文块（超 MAX_L2_DISTANCE 丢弃），去向 RAG prompt
          拼接 → multi_agent → SSE 推送前端。
    异常：embedding 维度不一致或远程调用失败时由 Chroma/DashScope 抛错，调用方兜底。
    """
    logger.info(f"\n🔍 检索，问题：{query}")
    where = build_scope_filter(user_id)
    results = db.similarity_search_with_score(query, k=top_k, filter=where)
    context=[]
    for doc,score in results:
        # L2 距离：超过上限视为不相关，丢弃
        if score > MAX_L2_DISTANCE:
            continue
        context.append(doc)
        # 过滤后可能少于 top_k，凑满即停
        if len(context) >= top_k:
            break
    return context


def test_retrieval_temp(db: Chroma, query: str, top_k: int = 3)->list[Document]:
    """会话临时知识库检索（纯内存库，整个 collection 均属于当前会话，无需 scope 过滤）。
    临时文件为用户在当前会话主动上传、库规模小，直接取最相似的 top_k，
    不套用持久化库的绝对距离阈值（避免短查询被杂糅 chunk 稀释后漏召回）。

    被谁调用：预留的会话临时库检索入口（线上等价逻辑在 app/domain/agents/retrieval.py
    对 TempKnowledgeStore 内存库的检索分支）。
    参数：db —— app/infrastructure/vector_store/temp_store.get_db 产出的会话级 Chroma；
          query —— 用户问题；top_k —— 返回块数上限。
    返回：list[Document]，由 service 层与持久化库结果合并后进入 RAG prompt。
    """
    results = db.similarity_search_with_score(query, k=top_k)
    context=[]
    for doc,score in results:
        context.append(doc)
        if len(context) >= top_k:
            break
    return context


# ====================== 主流程（一键运行） ======================
if __name__ == "__main__":
    # 1. 你的JSON文件路径（直接对应你项目里的路径）
    json_path = "../data/LearnPlan_Dialogue_Collection/LearnPlan_Dialogue_Collection.json"
    
    # 2. 加载JSON
    json_data = load_json_data(json_path)
    
    # 3. 转Document
    docs = json_to_documents(json_data)
    
    # 4. 文本分块
    splitted_docs = split_documents(docs)
    
    # 5. 加载带缓存的Embedding模型
    embeddings = get_embedding()
    
    # 6. 构建Chroma向量库（持久化目录取 settings.CHROMA_DIR，默认 storage/chromadb）
    db = build_chromadb(splitted_docs, embeddings)
    
    # 7. 测试检索（可自定义问题）
    test_retrieval(db, "高一学生语文跟不上怎么办？")
    
