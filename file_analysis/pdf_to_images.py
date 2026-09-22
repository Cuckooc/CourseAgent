"""
模块名：pdf_to_images.py（PDF 逐页渲染为 PNG 临时图片，供视觉模型识别）

作用：
    用 PyMuPDF (fitz) 把 PDF 每一页按指定 DPI 光栅化为 PNG 临时文件，
    作为 OCR / 多模态提取的图片输入；同时提供临时文件清理工具，
    保证图片只在识别调用期间短暂落盘。

主要成员：
    - pdf_to_images：模块主入口，PDF → 每页一张 PNG，返回路径列表；
    - cleanup_images：删除临时 PNG，并顺带删除已空的临时目录。

被谁使用：
    - file_analysis/ocr_service.py 的 ocr_pdf()：扫描件 OCR 路径，
      以默认 DPI 调 pdf_to_images，finally 中调 cleanup_images
      （文件.函数：ocr_service.ocr_pdf）；
    - file_analysis/multimodal_service.py 的 extract_with_multimodal()：
      image_rich 多模态路径，显式以 dpi=200 调用，finally 中清理
      （文件.函数：multimodal_service.extract_with_multimodal）。
"""
from __future__ import annotations

import logging
import os
import tempfile
from typing import List

logger = logging.getLogger(__name__)

try:
    import fitz
except ImportError:
    fitz = None
    logger.warning("PyMuPDF not installed — pdf_to_images unavailable")


def pdf_to_images(file_path: str, dpi: int = 200) -> List[str]:
    """将 PDF 每页渲染为 PNG，返回临时文件路径列表（图片即 OCR 输入）。

    功能：在系统临时目录下创建 pdf_img_ 前缀的唯一临时目录，用
    PyMuPDF 按 dpi 计算缩放矩阵逐页光栅化，输出 page_0001.png 起的
    连续命名图片；页数 = fitz 打开后的 PDF 总页数（逐页全部渲染，
    不做采样）。
    被谁调用：
    - file_analysis/ocr_service.py 的 ocr_pdf()（默认 dpi=200）；
    - file_analysis/multimodal_service.py 的 extract_with_multimodal()
      （显式 dpi=200）。
    参数：
    - file_path (str)：已落盘 PDF 的本地路径，来源：
      service/file_service._extract_text 传入的上传文件物理路径；
    - dpi (int)：渲染分辨率，默认 200。DPI 200 在 OCR 精度与
      内存/体积间平衡（A4 页 ≈ 1654×2339 px）；fitz 以 72 DPI 为
      基准点，故缩放系数 zoom = dpi / 72。
    返回：
    - List[str]：与 PDF 页序一致的临时 PNG 绝对路径列表，去向：
      调用方逐页交给 qwen-vl-plus / qwen-vl-max 识别；调用方须在
      使用完毕后调 cleanup_images() 清理（两处调用均放在 finally 中）。
    异常：
    - 未安装 PyMuPDF：抛 RuntimeError；
    - PDF 打开/渲染失败：先清理已生成图片与临时目录再原样抛出，
      避免磁盘泄漏。
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for pdf_to_images")

    # mkdtemp 自动生成唯一目录，避免并发上传渲染时临时文件互相覆盖
    tmp_dir = tempfile.mkdtemp(prefix="pdf_img_")
    image_paths = []

    try:
        doc = fitz.open(file_path)
        # fitz 坐标以 72 DPI 为基准：zoom=dpi/72 把页面等比放大到目标 DPI
        zoom = dpi / 72.0
        mat = fitz.Matrix(zoom, zoom)

        # 页数：PDF 全部页逐页渲染（doc 长度即总页数），不做采样/截断
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            pix = page.get_pixmap(matrix=mat)
            # 页码 1 起、宽度 4 补零命名，保证按文件名排序即按页序
            out_path = os.path.join(tmp_dir, f"page_{page_idx + 1:04d}.png")
            pix.save(out_path)
            image_paths.append(out_path)

        doc.close()
    except Exception:
        # 渲染中途失败：清理已落盘图片与空目录后把异常抛给调用方
        cleanup_images(image_paths)
        if os.path.exists(tmp_dir):
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass
        raise

    logger.info("Rendered %d pages from %s to %s", len(image_paths), file_path, tmp_dir)
    return image_paths


def cleanup_images(image_paths: List[str]) -> None:
    """删除 pdf_to_images 生成的临时 PNG，并顺带删除已清空的临时目录。

    被谁调用：
    - ocr_service.ocr_pdf() / multimodal_service.extract_with_multimodal()
      的 finally 块（正常完成与单页降级时都执行）；
    - pdf_to_images() 自身渲染失败的 except 分支。
    参数：
    - image_paths (List[str])：pdf_to_images 返回的临时 PNG 路径列表，
      允许包含已不存在的路径。
    返回：
    - None。单文件删除失败仅记 warning 不抛出（清理动作不能掩盖
      主流程异常）；目录仅在确认为空目录时移除，非空（如混入其他
      临时文件）则保留。
    """
    # 用 set 收集图片所在目录：一次渲染的图片通常同目录，去重后统一删目录
    dirs_to_remove = set()
    for path in image_paths:
        try:
            if os.path.exists(path):
                os.remove(path)
            dirs_to_remove.add(os.path.dirname(path))
        except OSError as e:
            logger.warning("Failed to remove temp image %s: %s", path, e)
    for d in dirs_to_remove:
        try:
            if d and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except OSError:
            pass
