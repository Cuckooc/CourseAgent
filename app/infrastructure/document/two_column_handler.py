"""
模块名：two_column_handler.py（双栏 PDF 的栏检测与阅读顺序还原）

作用：
    用 PyMuPDF (fitz) 的 get_text("dict") 获取页面 text blocks 及其
    bbox 坐标，通过坐标分析识别双栏排版，并按人类阅读顺序
    「先左栏后右栏、栏内从上到下」拼接文本，解决 PyPDF2 直接抽取
    双栏 PDF 时左右栏行交错串行的问题；单栏页则按 y 坐标从上到下
    正常提取。

双栏判定规则（_detect_columns）：
    以页中线 ±5% 页宽为容差带把 block 分为左/右/跨中三组：
    存在跨中 block、任一栏为空、任一栏少于 2 个 block 均判单栏；
    否则要求栏间中缝 > 页宽 5%（_COLUMN_GAP_MIN_RATIO）且左右栏
    block 数量差占比 < 30%（_COLUMN_BALANCE_RATIO，排除单栏缩进
    误判）才判双栏。

主要成员：
    - _detect_columns：单页栏布局检测，返回 (是否双栏, 中缝占比)；
    - _extract_page_ordered：双栏页按阅读顺序提取；
    - _extract_page_normal：单栏页从上到下提取；
    - extract_two_column：模块对外主入口，逐页检测并提取全书。

被谁使用：
    - app/application/files/file_service.py 的 _extract_text()：doc_type 为
      "two_column" 时延迟导入并调用 extract_two_column
      （文件.函数：file_service._extract_text）；产出文本随后进入
      切分 → embedding → 向量库主链路。
"""
from __future__ import annotations

import logging
from typing import Tuple

logger = logging.getLogger(__name__)

try:
    import fitz
except ImportError:
    fitz = None
    logger.warning("PyMuPDF not installed — two_column_handler unavailable")

# 栏检测参数
_COLUMN_GAP_MIN_RATIO = 0.05   # 栏间距 > 页宽 5% 才判定为双栏
_COLUMN_BALANCE_RATIO = 0.3    # 两栏 block 数量差 < 30% 才视为均衡双栏


def _detect_columns(page) -> Tuple[bool, float]:
    """检测单个页面是否为双栏布局，并返回中缝占页宽比例。

    被谁调用：extract_two_column() 逐页调用，据此选择双栏/单栏提取器。
    参数：
    - page：fitz.Page 对象（extract_two_column 打开的 PDF 页）。
    返回：
    - Tuple[bool, float]：(is_two_column, gap_ratio)。判定流程：
      取 type=0 文本 block，少于 4 个返回 (False, 0.0)；按 block
      bbox 相对页中线的位置分左/右/跨中三组；存在跨中 block（如
      通栏标题/宽表格）或缺任一栏直接判单栏；中缝 = 右栏最小左边界
      − 左栏最大右边界；中缝占比 > 5%、左右 block 数量差占比 < 30%、
      两栏各至少 2 个 block 时才返回 (True, gap_ratio)。
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
    # type==0 为文本 block；少于 4 个不具备统计分栏的意义
    text_blocks = [b for b in blocks if b.get("type") == 0]
    if len(text_blocks) < 4:
        return False, 0.0

    page_width = page.rect.width
    mid = page_width / 2

    # 魔数 0.05：页中线两侧各 5% 页宽的容差带；bbox 右界不超过
    # 中线+5% 归左栏，左界不小于中线-5% 归右栏，横跨容差带的
    # block（通栏标题/宽表格）归入 center_blocks
    left_blocks = [b for b in text_blocks if b["bbox"][2] <= mid + page_width * 0.05]
    right_blocks = [b for b in text_blocks if b["bbox"][0] >= mid - page_width * 0.05]
    center_blocks = [b for b in text_blocks
                     if b["bbox"][0] < mid - page_width * 0.05
                     and b["bbox"][2] > mid + page_width * 0.05]

    # 存在跨中 block 说明页内有通栏内容，按单栏处理更安全
    if center_blocks:
        return False, 0.0

    if not left_blocks or not right_blocks:
        return False, 0.0

    max_left = max(b["bbox"][2] for b in left_blocks)
    min_right = min(b["bbox"][0] for b in right_blocks)
    gap = min_right - max_left
    gap_ratio = gap / page_width

    # balance：两栏 block 数量差占总数比例，越大越不均衡（像单栏缩进）
    total = len(left_blocks) + len(right_blocks)
    balance = abs(len(left_blocks) - len(right_blocks)) / total if total else 1.0

    is_two_col = (
        gap_ratio > _COLUMN_GAP_MIN_RATIO
        and balance < _COLUMN_BALANCE_RATIO
        and len(left_blocks) >= 2
        and len(right_blocks) >= 2
    )

    return is_two_col, gap_ratio


def _extract_page_ordered(page) -> str:
    """双栏页按正确阅读顺序提取：先左栏后右栏，栏内从上到下。

    被谁调用：extract_two_column() 中 _detect_columns 判为双栏的页。
    参数：
    - page：fitz.Page 对象。
    返回：
    - str：阅读顺序还原后的页面文本，行内 span 直接拼接、行间 \\n、
      空白行丢弃；无任何文本 block 时返回空串。
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
    text_blocks = [b for b in blocks if b.get("type") == 0]

    if not text_blocks:
        return ""

    page_width = page.rect.width
    mid = page_width / 2

    # 分栏口径与 _detect_columns 一致（中线 ±5% 容差带）；
    # 排序键 (y0, x0)：栏内先按纵向顶边从上到下，同行再按横从左到右
    left = sorted(
        [b for b in text_blocks if b["bbox"][2] <= mid + page_width * 0.05],
        key=lambda b: (b["bbox"][1], b["bbox"][0]),
    )
    right = sorted(
        [b for b in text_blocks if b["bbox"][0] >= mid - page_width * 0.05],
        key=lambda b: (b["bbox"][1], b["bbox"][0]),
    )

    lines = []
    # left + right 即阅读顺序：左栏读完再读右栏，避免行交错串行
    for b in left + right:
        for line in b.get("lines", []):
            # 一行由多个 span 组成（字体/字号变化处切分），无分隔直接拼接
            line_text = "".join(span.get("text", "") for span in line.get("spans", []))
            if line_text.strip():
                lines.append(line_text)

    return "\n".join(lines)


def _extract_page_normal(page) -> str:
    """普通单栏页面提取：所有 block 按 y 坐标从上到下排序。

    被谁调用：extract_two_column() 中 _detect_columns 判为单栏的页。
    参数：
    - page：fitz.Page 对象。
    返回：
    - str：从上到下顺序的页面文本，行间 \\n，空白行丢弃。
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
    # 排序键 (y0, x0)：纵向顶边为主序，同一高度带再按横向从左到右
    text_blocks = sorted(
        [b for b in blocks if b.get("type") == 0],
        key=lambda b: (b["bbox"][1], b["bbox"][0]),
    )

    lines = []
    for b in text_blocks:
        for line in b.get("lines", []):
            line_text = "".join(span.get("text", "") for span in line.get("spans", []))
            if line_text.strip():
                lines.append(line_text)

    return "\n".join(lines)


def extract_two_column(file_path: str) -> str:
    """双栏 PDF → 正确阅读顺序的文本（模块对外主入口）。

    功能：打开 PDF 后逐页检测栏布局——双栏页走 _extract_page_ordered
    （先左栏后右栏、栏内从上到下），单栏页走 _extract_page_normal
    （从上到下）；各非空页文本以 \\n\\n 拼接为全书文本。
    被谁调用：app/application/files/file_service.py 的 _extract_text()，
    doc_type 判定为 "two_column" 时延迟导入并调用
    （文件.函数：file_service._extract_text）；返回文本随后进入
    切分 → embedding → 向量库主链路。
    参数：
    - file_path (str)：已落盘 PDF 的本地路径，来源：file_control
      保存的前端上传物理文件路径，由 file_service 传入。
    返回：
    - str：阅读顺序正确的全书纯文本；所有页均无文本时返回空串，
      由上层报“未解析到文本内容”。
    异常：
    - 未安装 PyMuPDF：抛 RuntimeError；
    - fitz.open 失败：包装为 RuntimeError 抛出，归入上层上传失败降级。
    """
    if fitz is None:
        raise RuntimeError("PyMuPDF is required for two_column extraction")

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        raise RuntimeError(f"Failed to open PDF: {e}")

    pages_text = []
    try:
        # 逐页检测：同一 PDF 内允许双栏页与单栏页混用，各页选对应提取器
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            is_two_col, gap_ratio = _detect_columns(page)

            if is_two_col:
                logger.debug("Page %d: two-column (gap=%.2f%%)", page_idx + 1, gap_ratio * 100)
                page_text = _extract_page_ordered(page)
            else:
                page_text = _extract_page_normal(page)

            # 空页不产生段落分隔，避免拼接后出现多余空行
            if page_text.strip():
                pages_text.append(page_text)
    finally:
        doc.close()

    # 页间空行拼接，与 file.pdf_text / ocr_service 的页边界口径一致
    return "\n\n".join(pages_text)
