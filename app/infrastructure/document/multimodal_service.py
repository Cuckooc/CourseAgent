"""
模块名：multimodal_service.py（image_rich PDF 的多模态内容提取服务）

作用：
    对 doc_type_detector 判定为 image_rich 的 PDF，逐页调用阿里百炼
    DashScope 的 qwen-vl-max 多模态大模型（MultiModalConversation API），
    将图片/表格丰富的页面转写为结构化文本：
- 表格 → Markdown 格式（保留表头，跨行表头合并为单层）；
- 图片 → 一句话主题描述 + 图中文字提取，以 [图片: ...] 标记；
- 混合页面 → 保持原始阅读顺序的结构化文本。

    页面图片由同包 pdf_to_images 以 DPI 200 渲染为临时 PNG，单页
    调用内置 tenacity 重试（3 次指数退避，2~10 秒），临时图片在
    finally 中无条件清理。

主要成员：
    - _MULTIMODAL_PROMPT_TABLE / _IMAGE / _MIXED：三类提示词；
    - _get_api_key / _get_multimodal_model：从环境变量读取 API Key、模型名；
    - _encode_image_base64：PNG → data URL 用 base64；
    - _extract_single_page：单页多模态提取（带 tenacity 重试）；
    - _detect_page_content_type：页面内容类型启发式（当前固定 mixed）；
    - extract_with_multimodal：模块对外主入口。

API Key 来源：
    读环境变量 api_key / DASHSCOPE_API_KEY；进程启动时
    config/setting.py 的 LLMConfig 通过 load_dotenv 加载
    env/qianwen_config.env 注入该变量（本地密钥文件，禁止入库）。

被谁使用：
    - app/application/files/file_service.py 的 _extract_text()：doc_type 为
      "image_rich" 时延迟导入并调用 extract_with_multimodal
      （文件.函数：file_service._extract_text）；产出文本随后进入
      切分 → embedding → 向量库主链路。
"""
from __future__ import annotations

import base64
import logging
import os
from typing import List, Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .pdf_to_images import pdf_to_images, cleanup_images

logger = logging.getLogger(__name__)

# 表格页提示词（魔数文案）：要求输出 Markdown 表格、合并跨行表头、
# 多表用 --- 分隔、只回表格不做解释，保证结果可直接作为文本入库
_MULTIMODAL_PROMPT_TABLE = (
    "请提取这张页面中的表格内容，输出为 Markdown 表格格式。\n"
    "要求：\n"
    "1. 保留完整的表头信息，如果表头跨多行，请合并为单层表头\n"
    "2. 表格内容保持原始顺序\n"
    "3. 如果页面有多个表格，用 --- 分隔\n"
    "4. 只输出表格，不要添加额外说明"
)

# 图片页提示词（魔数文案）：先一句话概括主题，再提取图中文字，
# 图片描述统一用 [图片: ...] 标记，便于后续清洗/检索区分图文
_MULTIMODAL_PROMPT_IMAGE = (
    "请描述这张图片的内容，并提取其中的文字信息。\n"
    "要求：\n"
    "1. 先用一句话概括图片主题\n"
    "2. 提取图片中的文字（如有），保持原始格式\n"
    "3. 如果是图表/流程图，描述其结构和关键信息\n"
    "4. 用 [图片: ...] 标记图片描述部分"
)

# 混合页提示词（魔数文案，最通用）：文本保结构、表格转 Markdown、
# 图片加 [图片: ...] 标记，并保持页面原有阅读顺序与层次
_MULTIMODAL_PROMPT_MIXED = (
    "请提取这张页面的全部内容，按以下规则输出：\n"
    "1. 普通文本：保持原始段落结构\n"
    "2. 表格：用 Markdown 表格格式，保留完整表头\n"
    "3. 图片：用 [图片: 描述内容] 标记，并提取图中文字\n"
    "4. 保持页面原有的阅读顺序和层次结构"
)


def _get_api_key() -> Optional[str]:
    """读取 DashScope API Key。

    被谁调用：extract_with_multimodal()。
    返回：环境变量 api_key 或 DASHSCOPE_API_KEY 的值（前者优先），
    均未配置时返回 None，由主入口判定后抛 RuntimeError 终止多模态路径。
    环境变量来源：config/setting.py 启动时 load_dotenv 加载的
    env/qianwen_config.env（LLMConfig.API_KEY 同源，密钥文件禁止入库）。
    """
    return os.getenv("api_key") or os.getenv("DASHSCOPE_API_KEY")


def _get_multimodal_model() -> str:
    """读取多模态模型名。

    被谁调用：extract_with_multimodal()。
    返回：环境变量 MULTIMODAL_MODEL，未配置时回退默认模型
    qwen-vl-max（强表格/图文理解能力，成本高于 OCR 用的 qwen-vl-plus）。
    """
    return os.getenv("MULTIMODAL_MODEL", "qwen-vl-max")


def _encode_image_base64(image_path: str) -> str:
    """把单张图片文件读取并编码为 base64 字符串。

    被谁调用：_extract_single_page()。
    参数：
    - image_path (str)：pdf_to_images 产出的临时 PNG 路径。
    返回：
    - str：可拼入 data:image/png;base64, URL 的 base64 字符串。
    """
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# tenacity 重试策略：任何异常最多重试 3 次，指数退避等待 2~10 秒，
# 最终仍失败则原样抛出（reraise=True），由主入口的单页降级逻辑接住
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _extract_single_page(image_path: str, api_key: str, model: str, prompt: str) -> str:
    """对单页 PNG 进行多模态提取，返回结构化文本（带 tenacity 重试）。

    被谁调用：extract_with_multimodal() 逐页循环调用。
    参数：
    - image_path (str)：pdf_to_images 产出的临时 PNG 路径；
    - api_key (str)：DashScope API Key，来自环境变量（env/qianwen_config.env）；
    - model (str)：多模态模型名（默认 qwen-vl-max）；
    - prompt (str)：按页面内容类型选定的提示词
      （_MULTIMODAL_PROMPT_TABLE / _IMAGE / _MIXED）。
    返回：
    - str：该页结构化文本（已 strip）。响应 content 为分段列表时
      拼接各段 text；兼容 output.text 等其他返回形态；未知结构
      兜底强转字符串，尽量不丢提取结果。
    异常：
    - HTTP 状态码非 200 时抛 RuntimeError，触发 tenacity 重试；
      3 次仍失败则向上抛出，由主入口记为该页失败标记、不中断整体。
    """
    # 延迟导入：仅真正走多模态路径时才加载 dashscope SDK
    import dashscope
    from dashscope import MultiModalConversation

    img_b64 = _encode_image_base64(image_path)

    messages = [
        {
            "role": "user",
            "content": [
                {"image": f"data:image/png;base64,{img_b64}"},
                {"text": prompt},
            ],
        }
    ]

    response = MultiModalConversation.call(
        model=model,
        messages=messages,
        api_key=api_key,
    )

    if response.status_code != 200:
        raise RuntimeError(
            f"Multimodal API returned {response.status_code}: {getattr(response, 'message', 'unknown')}"
        )

    output = response.output
    # 主流返回形态：choices[0].message.content，可能是 list[dict] 或 str
    if hasattr(output, "choices") and output.choices:
        content = output.choices[0].message.content
        if isinstance(content, list):
            # 多模态分段响应：抽取每个 dict 中的 text 段并按换行拼接
            text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and "text" in item]
            return "\n".join(text_parts).strip()
        return str(content).strip()
    # 兼容形态：模型直接把文本挂在 output.text 上
    if hasattr(output, "text"):
        return output.text.strip()
    # 兜底：未知响应结构时强转字符串，尽量不丢提取结果
    return str(output).strip()


def _detect_page_content_type(image_path: str) -> str:
    """简单启发式判断页面内容类型（用于选择 prompt）。

    被谁调用：extract_with_multimodal() 每页提取前调用。
    参数：
    - image_path (str)：当前页临时 PNG 路径（形参保留，当前实现
      不真正分析图片内容）。
    返回：
    - str：当前固定返回 "mixed"，即所有页面统一使用最通用的
      _MULTIMODAL_PROMPT_MIXED；table/image 分支为后续按图选型
      预留。
    """
    return "mixed"


def extract_with_multimodal(file_path: str, pages: Optional[List[int]] = None) -> str:
    """image_rich PDF → 逐页多模态提取 → 拼接结构化全文（模块对外主入口）。

    被谁调用：app/application/files/file_service.py 的 _extract_text()，
    doc_type 判定为 "image_rich" 时延迟导入并调用
    （文件.函数：file_service._extract_text）；返回文本随后进入
    切分 → embedding → 向量库主链路。
    流程：
    1. pdf_to_images 以 DPI 200 渲染全部页为临时 PNG；
    2. 逐页（或 pages 指定页）选择 prompt 调 qwen-vl-max，
       单页内置 3 次指数退避重试；
    3. 每页结果加 <!-- Page N --> 页码标记后按 \\n\\n 拼接；
    4. 无论成功失败都在 finally 中清理临时图片。
    参数：
    - file_path (str)：已落盘 PDF 的本地路径，由 file_service 传入
      （最初来自 file_control 保存的前端上传文件）；
    - pages (Optional[List[int]])：指定页码列表（0 起始），
      None 表示全部页面；越界页码静默跳过。
    返回：
    - str：多模态提取的 Markdown 结构化文本；单页重试 3 次仍失败
      时该页以失败标记占位、不中断整本；无页可渲染时返回空串，
      由上层报“未解析到文本内容”。
    异常：
    - 未配置 API Key：抛 RuntimeError，归入上层上传失败降级；
    - PDF 转图失败：异常向上抛出（临时文件已在 pdf_to_images 内清理）。
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("DashScope API key not configured for multimodal extraction")

    model = _get_multimodal_model()
    # DPI 200：与 OCR 路径一致的识别精度/体积平衡点
    image_paths = pdf_to_images(file_path, dpi=200)

    if not image_paths:
        return ""

    page_texts = []
    try:
        total = len(image_paths)
        # pages 为 None 时覆盖全部页；否则只处理调用方指定的 0 起始页码
        target_pages = pages if pages is not None else range(total)

        for i in target_pages:
            # 越界页码静默跳过，不影响其他页
            if i < 0 or i >= total:
                continue
            img_path = image_paths[i]
            content_type = _detect_page_content_type(img_path)

            # 按内容类型选提示词；当前检测器固定返回 mixed
            if content_type == "table":
                prompt = _MULTIMODAL_PROMPT_TABLE
            elif content_type == "image":
                prompt = _MULTIMODAL_PROMPT_IMAGE
            else:
                prompt = _MULTIMODAL_PROMPT_MIXED

            try:
                text = _extract_single_page(img_path, api_key, model, prompt)
                # HTML 注释样式页码标记：保留页序信息且通常不干扰正文检索
                page_texts.append(f"<!-- Page {i + 1} -->\n{text}")
                logger.info("Multimodal page %d/%d: %d chars", i + 1, total, len(text))
            except Exception as e:
                # 单页降级：重试 3 次仍失败的页只留失败标记，不拖垮整本 PDF
                logger.warning("Multimodal extraction failed for page %d: %s", i + 1, e)
                page_texts.append(f"<!-- Page {i + 1}: extraction failed -->")
    finally:
        # 无条件清理临时 PNG 与临时目录，避免磁盘泄漏
        cleanup_images(image_paths)

    return "\n\n".join(t for t in page_texts if t)
