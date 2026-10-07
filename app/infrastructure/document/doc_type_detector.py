"""
模块名：doc_type_detector.py（PDF 文档类型的启发式检测）

作用：
    根据抽样页面的文本密度、乱码比例、图片面积占比与栏布局，
    将 PDF 分为四类，供 app/application/files/file_service 路由到对应解析器：
- pure_text  ：文本层完整，直接用 PyPDF2 提取（file.pdf_text）；
- scanned    ：扫描件，文本层为空或乱码，走 OCR（ocr_service.ocr_pdf
               → ocr_clean.clean_ocr_text）；
- two_column ：双栏排版，走按栏排序提取（two_column_handler.extract_two_column）；
- image_rich ：图片/表格丰富，走多模态提取（multimodal_service.extract_with_multimodal）。

判定顺序（detect_pdf_type 内）：scanned → image_rich → two_column →
pure_text，先命中先返回；任一步检测失败均兜底返回 "pure_text"，
由上层 pdf_text() 用 PyPDF2 直接抽取文本层兜底。依赖 PyMuPDF (fitz)。

主要成员：
    - _MAX_SAMPLE_PAGES 等模块级魔数阈值；
    - _is_garbage_char / _GARBAGE_RE / _garbage_ratio：乱码判定；
    - _detect_two_column：单页双栏布局检测（按 block 的 x 坐标分桶）；
    - detect_pdf_type：模块对外主入口，输出四类标签之一。

被谁使用：
    - app/application/files/file_service.py 的 _extract_text()：PDF 分支第一步调用
      detect_pdf_type，再按返回标签分发解析器
      （文件.函数：file_service._extract_text）。
"""
from __future__ import annotations

import logging
import re
from typing import List

logger = logging.getLogger(__name__)

try:
    import fitz  # PyMuPDF
except ImportError:
    fitz = None
    logger.warning("PyMuPDF not installed — doc_type_detector will always return 'pure_text'")

# 最多分析前 N 页（大文件不逐页扫描）
_MAX_SAMPLE_PAGES = 10

# 判定阈值
_TEXT_DENSITY_SCANNED = 5.0       # 每页文本字符数 / 页面面积(in²) 低于此值视为"几乎无文本"
_GARBAGE_RATIO_SCANNED = 0.3     # 乱码字符占比超此值视为扫描件
_IMAGE_AREA_RATIO = 0.30          # 图片面积占页面面积超此值视为 image_rich
_COLUMN_GAP_MIN_RATIO = 0.05     # 栏间距 > 页面宽度 5% 才判定为双栏


def _is_garbage_char(ch: str) -> bool:
    """判断单个字符是否为乱码（非中英文、非标点、非空白控制符）。

    被谁调用：detect_pdf_type() 抽样统计乱码字符数；
    _garbage_ratio() 亦通过它逐字符计数。
    参数：
    - ch (str)：PDF 文本层抽出的单个字符。
    返回：
    - bool：码点低于 0x20 的非空白控制符判为乱码；CJK 汉字
      （\\u4e00-\\u9fff）、可打印 ASCII（0x20-0x7e）、CJK 标点
      （\\u3000-\\u303f）、全角字符（\\uff00-\\uffef）判为正常；
      其余码点（私用区符号、字形替换符等扫描件文本层典型噪声）
      判为乱码。
    """
    if ch in ('\n', '\r', '\t', ' '):
        return False
    cp = ord(ch)
    if cp < 0x20:
        return True
    if '\u4e00' <= ch <= '\u9fff':
        return False
    if '\u0020' <= ch <= '\u007e':
        return False
    if '\u3000' <= ch <= '\u303f':
        return False
    if '\uff00' <= ch <= '\uffef':
        return False
    return True


# 乱码字符正则：与 _is_garbage_char 判定区间等价的预编译版本，
# 白名单 = CJK 汉字 + 可打印 ASCII + CJK 标点 + 全角字符 + 换行/回车/制表符
_GARBAGE_RE = re.compile(r'[^\u4e00-\u9fff\u0020-\u007e\u3000-\u303f\uff00-\uffef\n\r\t]')


def _garbage_ratio(text: str) -> float:
    """计算文本中乱码字符占比（0.0~1.0）。

    被谁调用：当前主链路的乱码统计内联在 detect_pdf_type() 中，
    本函数为等价的工具口径保留函数（可复用/测试）。
    参数：
    - text (str)：待统计文本（通常为单页或抽样页拼接的文本层内容）。
    返回：
    - float：乱码字符数 / 文本长度；空文本返回 0.0。分母取
      max(len, 1) 防止除零。
    """
    if not text:
        return 0.0
    garbage = sum(1 for ch in text if _is_garbage_char(ch))
    return garbage / max(len(text), 1)


def _detect_two_column(page) -> bool:
    """检测单个页面是否为双栏布局（判定规则：x 坐标分桶 + 中缝宽度）。

    被谁调用：detect_pdf_type() 对每个抽样页调用，统计双栏页占比。
    参数：
    - page：fitz.Page 对象（由 detect_pdf_type 打开的 PDF 页）。
    返回：
    - bool：取 get_text("dict") 的 type=0 文本 block，少于 4 个直接
      判单栏；按 block 左右边界分桶——x0 在页宽 45% 以左入左桶、
      x1 在 55% 以右入右桶（魔数 0.45/0.55 为中缝容差带），两桶各
      至少 2 个 block，且左桶最大右边界与右桶最小左边界之间的中缝
      宽度 > 页宽 5%（_COLUMN_GAP_MIN_RATIO）时判定为双栏。
    """
    blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_WHITESPACE).get("blocks", [])
    # type==0 表示文本 block（排除图片 block 等）；块太少不足以证明分栏结构
    text_blocks = [b for b in blocks if b.get("type") == 0]
    if len(text_blocks) < 4:
        return False

    page_width = page.rect.width
    xs = [(b["bbox"][0], b["bbox"][2]) for b in text_blocks]
    # 魔数 0.45/0.55：以页中线两侧各 5% 宽作为中缝容差带，
    # 左边界落在 45% 以左归左栏候选，右边界落在 55% 以右归右栏候选
    left_xs = sorted([x0 for x0, _ in xs if x0 < page_width * 0.45])
    right_xs = sorted([x1 for _, x1 in xs if x1 > page_width * 0.55])

    if len(left_xs) < 2 or len(right_xs) < 2:
        return False

    max_left = max(left_xs) if left_xs else 0
    min_right = min(right_xs) if right_xs else page_width
    # 中缝 = 右栏最靠左的左边界 - 左栏最靠右的右边界
    gap = min_right - max_left
    return gap > page_width * _COLUMN_GAP_MIN_RATIO


def detect_pdf_type(file_path: str) -> str:
    """检测 PDF 文档类型（模块对外主入口）。

    功能：只抽样分析前 _MAX_SAMPLE_PAGES 页（大文件不逐页扫描），
    汇总文本密度（字符数/页面面积平方英寸）、乱码占比、图片像素
    面积占页面面积比、双栏页占比四项指标后按固定优先级判定。
    判定规则（顺序即优先级，先命中先返回）：
    1. 文本密度 < 5.0 且乱码占比 > 0.3 → "scanned"（几乎无文本层）；
    2. 图片面积占比 > 0.30 → "image_rich"；
    3. 双栏页占抽样页比例 ≥ 0.5 → "two_column"；
    4. 以上均不满足 → "pure_text"。
    被谁调用：app/application/files/file_service.py 的 _extract_text()，PDF 分支
    第一步（文件.函数：file_service._extract_text）；返回标签决定
    后续走 PyPDF2 / OCR / 双栏 / 多模态哪条解析路径。
    参数：
    - file_path (str)：已落盘 PDF 的本地路径，来源：file_control
      保存的前端上传物理文件路径，由 file_service 传入。
    返回：
    - str："pure_text" | "scanned" | "two_column" | "image_rich"，
      与提取文本一起由 file_service 回传上传接口展示。
    异常：
    - 未安装 fitz、fitz.open 失败或统计过程任意异常：均记录
      warning 后兜底返回 "pure_text"，交由 PyPDF2 文本层路径兜底。
    """
    if fitz is None:
        return "pure_text"

    try:
        doc = fitz.open(file_path)
    except Exception as e:
        logger.warning("fitz open failed for %s: %s, defaulting to pure_text", file_path, e)
        return "pure_text"

    try:
        n_pages = len(doc)
        sample_count = min(n_pages, _MAX_SAMPLE_PAGES)

        total_text_len = 0
        total_page_area = 0.0
        total_garbage_chars = 0
        total_image_area = 0.0
        total_real_area = 0.0
        two_col_page_count = 0

        for i in range(sample_count):
            page = doc[i]
            # 页面面积两种口径：点²/72/72 换算为平方英寸（供文本密度），
            # 点²原始值 total_real_area（与图片像素面积同口径比占比）
            page_area_in2 = page.rect.width * page.rect.height / 72.0 / 72.0
            total_real_area += page.rect.width * page.rect.height
            # 下限 0.01 平方英寸：防止异常零尺寸页把密度分母拉成 0
            total_page_area += max(page_area_in2, 0.01)

            text = page.get_text()
            total_text_len += len(text.strip())
            total_garbage_chars += sum(1 for ch in text if _is_garbage_char(ch))

            # 图片面积统计：按 xref 取嵌入图位图，累加像素宽×高；
            # 单图取出失败不影响整页指标（pass 跳过）
            for img in page.get_images(full=True):
                xref = img[0]
                try:
                    pix = fitz.Pixmap(doc, xref)
                    total_image_area += pix.width * pix.height
                except Exception:
                    pass

            if _detect_two_column(page):
                two_col_page_count += 1

        # 四项汇总指标：字符密度（每平方英寸字符数）、乱码占比、
        # 图片像素面积/页面面积、双栏页占比
        text_density = total_text_len / total_page_area
        total_chars = max(total_text_len, 1)
        garbage_ratio = total_garbage_chars / total_chars
        image_area_ratio = total_image_area / max(total_real_area, 1)

        col_ratio = two_col_page_count / sample_count

        logger.info(
            "PDF type detection for %s: density=%.1f, garbage=%.2f, image_ratio=%.2f, col_pages=%d/%d",
            file_path, text_density, garbage_ratio, image_area_ratio,
            two_col_page_count, sample_count,
        )

        # 判定优先级固定：扫描件 → 图文丰富 → 双栏 → 纯文本
        if text_density < _TEXT_DENSITY_SCANNED and garbage_ratio > _GARBAGE_RATIO_SCANNED:
            return "scanned"
        if image_area_ratio > _IMAGE_AREA_RATIO:
            return "image_rich"
        # 魔数 0.5：抽样页中至少一半判为双栏才整体按双栏处理
        if col_ratio >= 0.5:
            return "two_column"
        return "pure_text"

    except Exception as e:
        logger.warning("PDF type detection failed for %s: %s, defaulting to pure_text", file_path, e)
        return "pure_text"
    finally:
        doc.close()
