"""
模块名：ocr_clean.py（OCR 识别文本的正则清洗管线）

作用：
    对 ocr_service.ocr_pdf 产出的原始 OCR 文本做后处理，修复多模态
    识别常见噪声，为后续「切分 → embedding → 向量库」提供干净文本：
    1. 去除零宽字符与控制字符（保留 \\n \\r \\t）；
    2. 合并行内连续空格；
    3. 删除乱码行（中英文字符占比 < 50% 的整行丢弃）；
    4. 合并被 OCR 错误断行的同一段落；
    5. 折叠连续空行（最多保留一个空行作为段落边界）。

主要成员：
    - _ZERO_WIDTH_RE / _MULTI_SPACE_RE / _VALID_CHAR_RE：模块级预编译正则；
    - _is_garbage_line：按有效字符占比判定乱码行；
    - _should_merge_lines：按句末标点与行首字符判定相邻两行是否应合并；
    - clean_ocr_text：模块对外主入口，串起整条清洗管线。

被谁使用：
    - app/application/files/file_service.py 的 _extract_text()：doc_type 为 "scanned"
      时，先调 ocr_service.ocr_pdf 取原始 OCR 文本，再调 clean_ocr_text
      清洗（文件.函数：file_service._extract_text）；清洗后的纯文本
      进入 FileService 的切分 → embedding → 向量库主链路。
"""
from __future__ import annotations

import logging
import re
from typing import List

logger = logging.getLogger(__name__)

# 零宽字符 + 常见控制字符（保留 \n \r \t）
_ZERO_WIDTH_RE = re.compile(
    r'[\u200b\u200c\u200d\u200e\u200f\ufeff'
    r'\x00-\x08\x0b\x0c\x0e-\x1f\x7f]'
)

# 连续空格合并
_MULTI_SPACE_RE = re.compile(r'[ \t]+')

# 中英文标点 + 中英文字符
_VALID_CHAR_RE = re.compile(
    r'[\u4e00-\u9fff'          # CJK 统一汉字
    r'\u3000-\u303f'           # CJK 标点
    r'\uff00-\uffef'           # 全角 ASCII + 半角变体
    r'\u0020-\u007e'           # 基本 ASCII
    r'\u2000-\u206f'           # 通用标点
    r'\n\r\t]'
)


def _is_garbage_line(line: str) -> bool:
    """判断一行是否为乱码行（清洗规则之三）。

    被谁调用：clean_ocr_text() 的逐行处理循环。
    参数：
    - line (str)：OCR 文本按 \\n 切出的单行（尚未 strip 的原始行）。
    返回：
    - bool：空白行返回 False（空白行交给后续空行折叠逻辑处理，
      不在此丢弃）；非空白行中有效字符（中英文/全角标点/ASCII/
      通用标点，匹配 _VALID_CHAR_RE）占比 < 魔数阈值 0.5 时
      返回 True，由调用方整行删除。
    """
    stripped = line.strip()
    if not stripped:
        return False
    valid_count = sum(1 for ch in stripped if _VALID_CHAR_RE.match(ch))
    # 魔数 0.5：有效字符不足一半即视为乱码行（OCR 把污渍/底纹误识为符号的典型特征）
    return valid_count / len(stripped) < 0.5


def _should_merge_lines(prev_line: str, curr_line: str) -> bool:
    """判断相邻两行是否应合并为同一段落（清洗规则之四：错误断行还原）。

    被谁调用：clean_ocr_text() 的段落合并循环（prev 为已保留的上一行，
    curr 为当前行）。
    参数：
    - prev_line (str)：上一行文本（允许带尾部空白）；
    - curr_line (str)：当前行文本（允许带前导空白）。
    返回：
    - bool：任一行为空返回 False；前一行以句末标点
      （.。!！?？:：;；引号/括号收尾符）结尾时返回 False（前句已结束）；
      否则当前行首为小写英文字母或 CJK 汉字时返回 True，视为同一段落
      被 OCR/排版错误硬断行，由调用方直接拼接。
    """
    if not prev_line or not curr_line:
        return False
    prev_stripped = prev_line.rstrip()
    curr_stripped = curr_line.lstrip()
    if not prev_stripped or not curr_stripped:
        return False

    last_char = prev_stripped[-1]
    first_char = curr_stripped[0]

    # 句末标点集合（魔数）：同时收录中英文句号、叹号、问号、冒号、
    # 分号及引号/括号的右半部分；命中即视为上一句完整结束
    sentence_endings = set('.。!！?？:：;；"\'"）)】」』"')
    if last_char in sentence_endings:
        return False

    # 当前行首为小写字母或中文 → 可能是断行
    if first_char.islower() or '\u4e00' <= first_char <= '\u9fff':
        return True

    return False


def clean_ocr_text(text: str) -> str:
    """清洗 OCR 识别结果（模块对外主入口）。

    功能：按模块 docstring 列出的五步管线顺序处理：去零宽/控制字符 →
    行内空格合并与乱码行丢弃 → 错误断行合并 → 连续空行折叠。
    被谁调用：
    - app/application/files/file_service.py 的 _extract_text()：scanned 分支中
      ocr_service.ocr_pdf 返回后立即调用
      （文件.函数：file_service._extract_text）。
    参数：
    - text (str)：ocr_service.ocr_pdf 逐页识别后拼接的原始 OCR 文本，
      可能含零宽字符、乱码行、硬断行等噪声。
    返回：
    - str：清洗后的纯文本（首尾 strip），去向：FileService 的
      脱敏 → split_str 切分 → embedding → Chroma 向量库；
      入参为空/假值时直接返回空串，上层据此报“未解析到文本内容”。
    """
    if not text:
        return ""

    # 1. 去除零宽字符和控制字符
    text = _ZERO_WIDTH_RE.sub('', text)

    # 2. 按行处理
    lines = text.split('\n')
    cleaned_lines: List[str] = []

    for line in lines:
        # 合并行内连续空格
        line = _MULTI_SPACE_RE.sub(' ', line)

        # 跳过乱码行
        if _is_garbage_line(line):
            continue

        cleaned_lines.append(line)

    # 3. 合并被错误断行的段落
    merged_lines: List[str] = []
    for line in cleaned_lines:
        if merged_lines and _should_merge_lines(merged_lines[-1], line):
            # 合并：去掉前一行末尾空格，直接拼接当前行
            merged_lines[-1] = merged_lines[-1].rstrip() + line
        else:
            merged_lines.append(line)

    # 4. 去除连续空行（保留最多一个空行作为段落分隔）
    result_lines: List[str] = []
    prev_empty = False
    for line in merged_lines:
        is_empty = not line.strip()
        if is_empty:
            if prev_empty:
                continue
            prev_empty = True
        else:
            prev_empty = False
        result_lines.append(line)

    return '\n'.join(result_lines).strip()
