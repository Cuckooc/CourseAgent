"""
模块名：app.application.ports.document

作用：
    文档解析能力（PDF 文本抽取/OCR/双栏/多模态/切块）的运行时装配点
    （服务定位器）。api/application/domain 层的文件入库与文件问答流程
    统一经本模块获取解析原语，不直接 import app.infrastructure.document.*
    （分层守卫 RULES 禁止业务层依赖基础设施）；具体实现由组合根
    app/api/deps.py 在应用启动时注册（Port↔Adapter 装配）。

主要成员（register_xxx_ops 由组合根启动时调用一次，传实现模块对象）：
    - pdf_text(path)：PDF 文本抽取（document.file）；
    - split_str(text, ...)：文本切块（document.file）；
    - to_documents(chunks, ...)：切块包装为 Document（document.file）；
    - file_build_chromadb(docs, embedding_model, ...)：document.file 的
      建库函数（与 embeddings Port 的 build_chromadb 不同实现，故以
      file_ 前缀区分）；
    - detect_pdf_type(file_path)：PDF 类型判定（doc_type_detector）；
    - clean_ocr_text(text)：OCR 文本清洗（ocr_clean）；
    - ocr_pdf(file_path)：整页 OCR（ocr_service）；
    - extract_two_column(file_path)：双栏 PDF 有序抽取（two_column_handler）；
    - extract_with_multimodal(file_path, pages=None)：多模态抽取
      （multimodal_service）。

被谁使用：
    - 调用方：app/application/files/file_service.py、app/api/v1/files.py、
      app/domain/agents/file_agent.py；
    - 装配方：app/api/deps.py（import 时执行各 register_*）。
"""
from typing import Any

__all__ = [
    "register_document_file_ops",
    "register_doc_type_detector_ops",
    "register_ocr_clean_ops",
    "register_ocr_service_ops",
    "register_two_column_ops",
    "register_multimodal_ops",
    "pdf_text",
    "split_str",
    "to_documents",
    "file_build_chromadb",
    "detect_pdf_type",
    "clean_ocr_text",
    "ocr_pdf",
    "extract_two_column",
    "extract_with_multimodal",
]


# 已注册的实现模块（组合根装配前为 None）
_document_file_ops: Any = None
_doc_type_detector_ops: Any = None
_ocr_clean_ops: Any = None
_ocr_service_ops: Any = None
_two_column_ops: Any = None
_multimodal_ops: Any = None


def _unloaded(what: str, registrar: str) -> RuntimeError:
    """组装「未装配」错误：组合根未注册实现属启动期配置错误。"""
    return RuntimeError(
        "文档解析能力未装配：组合根 app/api/deps.py 未注册{}（{}）".format(what, registrar)
    )


def register_document_file_ops(ops: Any) -> None:
    """注册 document.file 模块（组合根启动时调用一次）。"""
    global _document_file_ops
    _document_file_ops = ops


def register_doc_type_detector_ops(ops: Any) -> None:
    """注册 doc_type_detector 模块（组合根启动时调用一次）。"""
    global _doc_type_detector_ops
    _doc_type_detector_ops = ops


def register_ocr_clean_ops(ops: Any) -> None:
    """注册 ocr_clean 模块（组合根启动时调用一次）。"""
    global _ocr_clean_ops
    _ocr_clean_ops = ops


def register_ocr_service_ops(ops: Any) -> None:
    """注册 ocr_service 模块（组合根启动时调用一次）。"""
    global _ocr_service_ops
    _ocr_service_ops = ops


def register_two_column_ops(ops: Any) -> None:
    """注册 two_column_handler 模块（组合根启动时调用一次）。"""
    global _two_column_ops
    _two_column_ops = ops


def register_multimodal_ops(ops: Any) -> None:
    """注册 multimodal_service 模块（组合根启动时调用一次）。"""
    global _multimodal_ops
    _multimodal_ops = ops


def pdf_text(*args: Any, **kwargs: Any) -> Any:
    """PDF 文本抽取（签名与行为同 document.file.pdf_text）。"""
    if _document_file_ops is None:
        raise _unloaded("PDF 文本抽取", "register_document_file_ops")
    return _document_file_ops.pdf_text(*args, **kwargs)


def split_str(*args: Any, **kwargs: Any) -> Any:
    """文本切块（签名与行为同 document.file.split_str）。"""
    if _document_file_ops is None:
        raise _unloaded("文本切块", "register_document_file_ops")
    return _document_file_ops.split_str(*args, **kwargs)


def to_documents(*args: Any, **kwargs: Any) -> Any:
    """切块包装为 Document（签名与行为同 document.file.to_documents）。"""
    if _document_file_ops is None:
        raise _unloaded("文档包装", "register_document_file_ops")
    return _document_file_ops.to_documents(*args, **kwargs)


def file_build_chromadb(*args: Any, **kwargs: Any) -> Any:
    """构建/加载 Chroma 库（签名与行为同 document.file.build_chromadb）。"""
    if _document_file_ops is None:
        raise _unloaded("文档建库", "register_document_file_ops")
    return _document_file_ops.build_chromadb(*args, **kwargs)


def detect_pdf_type(*args: Any, **kwargs: Any) -> Any:
    """PDF 类型判定（签名与行为同 doc_type_detector.detect_pdf_type）。"""
    if _doc_type_detector_ops is None:
        raise _unloaded("PDF 类型判定", "register_doc_type_detector_ops")
    return _doc_type_detector_ops.detect_pdf_type(*args, **kwargs)


def clean_ocr_text(*args: Any, **kwargs: Any) -> Any:
    """OCR 文本清洗（签名与行为同 ocr_clean.clean_ocr_text）。"""
    if _ocr_clean_ops is None:
        raise _unloaded("OCR 文本清洗", "register_ocr_clean_ops")
    return _ocr_clean_ops.clean_ocr_text(*args, **kwargs)


def ocr_pdf(*args: Any, **kwargs: Any) -> Any:
    """整页 OCR（签名与行为同 ocr_service.ocr_pdf）。"""
    if _ocr_service_ops is None:
        raise _unloaded("整页 OCR", "register_ocr_service_ops")
    return _ocr_service_ops.ocr_pdf(*args, **kwargs)


def extract_two_column(*args: Any, **kwargs: Any) -> Any:
    """双栏 PDF 有序抽取（签名与行为同 two_column_handler.extract_two_column）。"""
    if _two_column_ops is None:
        raise _unloaded("双栏抽取", "register_two_column_ops")
    return _two_column_ops.extract_two_column(*args, **kwargs)


def extract_with_multimodal(*args: Any, **kwargs: Any) -> Any:
    """多模态抽取（签名与行为同 multimodal_service.extract_with_multimodal）。"""
    if _multimodal_ops is None:
        raise _unloaded("多模态抽取", "register_multimodal_ops")
    return _multimodal_ops.extract_with_multimodal(*args, **kwargs)
