"""
模块名：file.py（file_analysis 包的基础文本提取与向量入库工具模块）

作用：
    承担上传解析流水线中「纯文本提取 → 文本切分 → Document 封装 →
    Chroma 向量入库」的基础环节。整条流水线位置为：
    前端上传 → control/file_control → service/file_service → 文件类型探测
    （doc_type_detector）→ PDF 转图 / OCR / 双栏 / 多模态清洗
    （pdf_to_images / ocr_service / ocr_clean / two_column_handler /
    multimodal_service）→ 纯文本 → 本模块切分封装 → embedding → 向量库。

主要成员：
    - pdf_text：按扩展名分发提取文本。.pdf 用 PyPDF2 逐页抽取文本层；
      .txt/.md 直接读文件（utf-8 优先、gbk 回退）；
    - split_str：按段落空行与最大长度把纯文本切分为 chunk 列表；
    - to_documents：把 chunk 封装为 langchain Document，并写入
      scope / user_id / session_id / original_name 等元数据；
    - build_chromadb：分批把 Document 写入持久化 Chroma 向量库；
    - test_retrieval：仅本地一键自测使用的相似度检索辅助函数。

被谁使用：
    - service/file_service.py 的 _extract_text() 调用 pdf_text 处理
      txt/md 与 pure_text 类型 PDF（上传主链路）；
    - control/file_control.py 的 _process_saved_file() 对 temp 临时文件
      调 pdf_text 补提原文，用于用户偏好抽取；
    - embedding/parent_child.py 调 split_str 取父子切分的原子段落；
    - multi_agent/file_agent.py 调 pdf_text/split_str/to_documents/
      build_chromadb 构建文件问答向量库；
    - 本文件 __main__ 块为本地一键自测入口。
"""

from __future__ import annotations

import sys
import os
from pathlib import Path
# 把项目根目录（本文件所在目录的上一级）加入 sys.path，保证以脚本方式
# 直接运行本文件时也能 import 到 embedding 等项目内模块
current_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.append(os.path.dirname(current_dir)) 
from PyPDF2 import PdfReader
from embedding.text_embedding import get_embedding
from langchain_chroma import Chroma
from langchain_core.documents import Document
import os
import logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler()]  # 强制输出到终端
)
logger=logging.getLogger(__name__)
def  pdf_text(path:str)->str:
    """读取文件文本：按扩展名分发提取。

    流水线位置：文本提取环节的纯文本兜底路径。扫描件 / 双栏 / 图文混排
    PDF 在 service/file_service._extract_text 中已被路由到 OCR、双栏或
    多模态模块；进入本函数 PDF 分支的均为可直接抽取文本层的文件，
    输出纯文本向下交给 split_str 切分、再经 embedding 入向量库。

    功能：
    - .pdf：PdfReader 逐页提取文本层，页间以两个换行拼接；
    - .txt/.md：直接读取（utf-8 优先，gbk 回退，仍失败则忽略非法字节）；
    - 其余扩展名：不进入任何读取分支，最终返回空串，由上层统一报
      “未解析到文本内容”。

    被谁调用：
    - service/file_service.py：_extract_text() 处理 txt/md 及 pure_text
      类型 PDF（文件.函数：file_service._extract_text）；
    - control/file_control.py：_process_saved_file() 对 temp 文件补提
      原文做用户偏好抽取（文件.函数：file_control._process_saved_file）；
    - multi_agent/file_agent.py：FileAgent 构建文件向量库；
    - 本文件 __main__ 自测入口。

    参数：
    - path (str)：已落盘文件的本地路径。主链路由 file_service 传入
      file_control 保存后的物理文件绝对路径；自测时为脚本内给定路径。

    返回：
    - str：提取到的文本；解析为空或发生任何异常时返回空串（不抛出），
      上层据此走“解析为空”的失败降级。

    异常：
    - 文件损坏 / 加密 / PyPDF2 解析失败等均在函数内捕获，记录 error
      日志后返回空串，异常不向调用方扩散。
    """
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext in (".txt", ".md"):
            logger.info(f"开始读取文本文件：{path}")
            with open(path, "rb") as f:
                raw = f.read()
            # 编码候选：优先 utf-8；中文 Windows 旧文本常见 gbk，依次回退
            for enc in ("utf-8", "gbk"):
                try:
                    return raw.decode(enc)
                except UnicodeDecodeError:
                    continue
            # 两种编码都失败：忽略非法字节强制解码，尽量保住可读内容
            return raw.decode("utf-8", errors="ignore")
        logger.info(f"开始读取文件：{path}")
        abs_path = os.path.abspath(path)
        reader=PdfReader(abs_path)
        logger.info(f"成功读取文件：{path}")
        text_str=""
        # 逐页抽取文本层，每页结果之间补两个换行作为段落/页边界
        for page in reader.pages:
            test=page.extract_text()
            if test:
                text_str+=test+"\n\n"

        return text_str
    except Exception as e:
        logger.error(f"读取文件失败：{e}")
        return ""
def split_str(text:str,max_len=500,min_len=50)->list[str]:
    """把纯文本切分为 chunk（段落）列表，供向量化使用。

    流水线位置：文本提取之后、to_documents/embedding 之前的切分环节。
    上游输入为 pdf_text / OCR / 双栏 / 多模态产出的纯文本，下游输出
    交给 to_documents 封装，或被 embedding/parent_child 当作父子切分
    的原子段落。

    被谁调用：
    - embedding/parent_child.py：build_parent_child_chunks 内调用，
      split_str 的产出作为父块打包的原子单元
      （文件.函数：parent_child.build_parent_child_chunks）；
    - multi_agent/file_agent.py：FileAgent 构建文件向量库；
    - 本文件 __main__ 自测入口。

    参数：
    - text (str)：上游提取出的原始纯文本；
    - max_len (int)：单段最大字符数（魔数 500，兼顾检索粒度与上下文
      完整度），超长段落按该窗口等距硬切；
    - min_len (int)：预留的最小段长参数（默认 50；当前实现不参与判断，
      非空短段一律保留，避免“上传成功但零向量”的静默数据丢失）。

    返回：
    - list[str]：去空、去首尾空白后的文本块列表，顺序与原文一致。

    异常：
    - 切分过程中任何异常记录 error 日志后原样 raise，由调用方决定降级。
    """
    try:
        # 以空行（\n\n）作为天然段落边界做第一次粗切
        paragraphs=text.split("\n\n")
        
        clean_paragraphs=[]
        for paragraph in paragraphs:
            paragraph=paragraph.strip()
            if not paragraph:
                continue

            if len(paragraph)>max_len:
                # 超长段落按 max_len 窗口等距硬切（中文按字切分，不做词边界处理）
                for i in range(0, len(paragraph), max_len):
                    chunk = paragraph[i:i+max_len].strip()
                    if chunk:
                        clean_paragraphs.append(chunk)

            elif paragraph:
                # 非空段落全部保留：短内容丢弃会造成"上传成功但零向量"的静默数据丢失
                clean_paragraphs.append(paragraph)
        return clean_paragraphs
    except Exception as e:
        logger.error(f"分段失败：{text}")
        raise e
def to_documents(chunks: list[str], source: str, scope: str = "private", user_id: int = None, session_id: int = None, original_name: str = None) -> list[Document]:
    """把文本块列表封装为 langchain Document 列表，供向量库写入。

    流水线位置：切分之后、向量入库（build_chromadb）之前的封装环节；
    metadata 随向量一起持久化，是后续按 scope / user_id / session_id
    做权限过滤与知识库管理展示的依据。

    被谁调用：
    - multi_agent/file_agent.py：FileAgent 构建文件向量库
      （文件.类.方法：file_agent.FileAgent，内部调 to_documents）；
    - 本文件 __main__ 自测入口；
    - 上传主链路中 service/file_service 另有带 file_id/content_hash 的
      同类封装，本函数为 file_analysis 侧的基础版本。

    参数：
    - chunks (list[str])：split_str 产出的文本块；
    - source (str)：来源标识（通常为文件路径），强转为 str 后写入
      metadata["source"]；
    - scope (str)：知识库范围，public（公共，所有用户可检索）/
      private（用户私有）/ temp（会话临时）；
    - user_id (int|None)：归属用户 ID，来自登录态 JWT，None 时不写入；
    - session_id (int|None)：会话 ID，temp 范围文件由 file_service
      传入，None 时不写入；
    - original_name (str|None)：用户上传时的原始文件名（含扩展名），
      供知识库管理页展示，为假值时不写入。

    返回：
    - list[Document]：与 chunks 等长、同序的 Document 列表，每个
      Document 的 metadata 含 source/index/length/scope 及可选归属字段。
    """
    source = str(source)
    docs = []
    for i, content in enumerate(chunks):
        meta = {
            "source": source,
            "index": i,
            "length": len(content),
            "scope": scope,
        }
        if original_name:
            meta["original_name"] = original_name
        if user_id is not None:
            meta["user_id"] = user_id
        if session_id is not None:
            meta["session_id"] = session_id
        docs.append(Document(page_content=content, metadata=meta))
    return docs
def build_chromadb(docs:list[Document],embedding_model,persist_path:str="../chromadb_data/")->Chroma:
    """把 Document 列表分批写入持久化 Chroma 向量库。

    流水线位置：解析-切分-封装之后的向量入库终点；写入后文本即可被
    RAG 相似度检索命中。线上上传主链路统一走 service/vector_store 与
    embedding/text_embedding.build_chromadb（含进程写锁），本函数用于
    file_agent 与本地自测场景。

    被谁调用：
    - multi_agent/file_agent.py：FileAgent 构建/刷新文件向量库
      （文件.类.方法：file_agent.FileAgent）；
    - 本文件 __main__ 自测入口。

    参数：
    - docs (list[Document])：to_documents 产出的文档列表；
    - embedding_model：embedding 模型对象（get_embedding() 产出），
      Chroma 用它在写入时把 page_content 向量化；
    - persist_path (str)：向量库持久化目录，默认 ../chromadb_data/。

    返回：
    - Chroma：已写入文档的 Chroma 实例（可直接用于检索）。
    """
    logger.info('build_chromadb start')
    db=Chroma(
        embedding_function=embedding_model,
        persist_directory=persist_path
        
    )
    # 批大小魔数 25：分批写入控制单次 embed/写入体积，降低大批量请求超时风险
    batch_size=25
    total=len(docs)
    for i in range(0, total, batch_size):
        batch = docs[i:i+batch_size]
        db.add_documents(batch)
        logger.info(f"✅ 已插入 {min(i+batch_size, total)}/{total} 条向量")
    # 直连底层 collection 统计实际落库条数，用于日志核对
    count=db._collection.count()
    logger.info(f"build_chromadb success,total {count} documents")
    return db
def test_retrieval(db: Chroma, query: str, top_k: int = 3)->list[Document]:
    """本地自测 RAG 检索效果（非线上链路函数）。

    功能：对给定问题做带距离分数的相似度检索，按魔数阈值 0.5 过滤后
    返回至多 top_k 条 Document。
    被谁调用：仅本文件 __main__ 自测入口；线上检索口径以
    embedding/text_embedding.py 的 test_retrieval / 各 agent 检索器为准。

    参数：
    - db (Chroma)：build_chromadb 返回的向量库实例；
    - query (str)：测试用问题文本；
    - top_k (int)：最多返回条数，默认 3。

    返回：
    - list[Document]：过滤后的命中文档列表。
    """
    logger.info(f"\n🔍 测试检索，问题：{query}")
    results = db.similarity_search_with_score(query, k=top_k)
    context=[]
    # 距离阈值魔数 0.5：score 小于该值的命中被跳过（历史自测过滤口径）
    threshold=0.5
    for doc,score in results:
        if score<threshold:
            continue
        context.append(doc)
        if len(context) >= top_k:
            break

    return context

# ====================== 主流程（一键运行） ======================
if __name__ == "__main__":

    # 演示样本已移出源码目录（数据/源码分离），锚定项目根 data/samples
    pdf_path = Path(os.path.join(os.path.dirname(current_dir), "data", "samples", "1.pdf"))
    text = pdf_text(pdf_path)
    splitted_docs = split_str(text)
    embeddings = get_embedding()
    docs = to_documents(splitted_docs, pdf_path)
    db = build_chromadb(docs, embeddings, persist_path="../chromadb_data/")
    test_retrieval(db, "基于改进 Census 变换与梯度融合的立体匹配算法，在 Middlebury 数据集上的平均非遮挡区域误匹配率和全部区域误匹配率分别是多少")

    
    
    
 

    
    



