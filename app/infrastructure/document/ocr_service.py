"""
模块名：ocr_service.py（扫描件 PDF 的 OCR 文本提取服务）

作用：
    扫描件 PDF → 逐页渲染为 PNG → 逐页调用多模态大模型 OCR →
    拼接全文。使用阿里百炼 DashScope 的 qwen-vl-plus 多模态模型，
    通过 MultiModalConversation API 对每页 PNG 图片进行文字识别，
    单页调用内置 tenacity 重试（3 次指数退避，2~10 秒）。

    API Key 与模型名均读环境变量（api_key / DASHSCOPE_API_KEY /
    OCR_MODEL）；环境变量由 core/config.py 的 LLMConfig 在进程
    启动时通过 load_dotenv 加载 env/qianwen_config.env 注入
    （LLMConfig.API_KEY 同源，本地密钥文件禁止入库）。

流水线位置：
    文件类型探测（doc_type_detector 判定为 scanned）之后、OCR 结果
    正则清洗（ocr_clean.clean_ocr_text）之前。上游输入为
    app/application/files/file_service 传入的本地 PDF 路径，下游输出的原始 OCR
    文本交给 clean_ocr_text 清洗，再进入切分 → embedding → 向量库。

主要成员：
    - _get_api_key / _get_ocr_model：从环境变量读取 API Key 与模型名；
    - _encode_image_base64：把 PNG 文件编码为 data URL 用的 base64；
    - _ocr_single_page：单页图片 OCR（带 tenacity 重试）；
    - ocr_pdf：模块对外主入口，PDF → 逐页 OCR → 拼接全文。

被谁使用：
    - app/application/files/file_service.py 的 _extract_text()：doc_type 为
      "scanned" 时调用 ocr_pdf（文件.函数：file_service._extract_text）；
    - 同包 pdf_to_images.py 提供 PDF 转图能力（本模块导入）。
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from .pdf_to_images import pdf_to_images, cleanup_images

logger = logging.getLogger(__name__)

# OCR 提示词（魔数文案）：要求保真段落结构、表格转 Markdown、图片给简述，
# 且只回识别内容不带额外解释，保证输出可直接进入后续文本清洗
_OCR_PROMPT = (
    "请识别并提取这张文档图片中的全部文字内容，保持原始段落结构和排版层次。"
    "如果页面包含表格，请用 Markdown 表格格式输出。"
    "如果页面包含图片，请简要描述图片内容。"
    "只输出识别到的内容，不要添加额外说明。"
)


def _get_api_key() -> Optional[str]:
    """读取 DashScope API Key。

    被谁调用：ocr_pdf()。返回：环境变量 api_key 或 DASHSCOPE_API_KEY
    的值（前者优先），均未配置时返回 None，由 ocr_pdf 判定后抛
    RuntimeError 终止 OCR 路径。环境变量来源：core/config.py 启动
    时 load_dotenv 加载的 env/qianwen_config.env（LLMConfig.API_KEY
    同源，密钥文件禁止入库）。
    """
    return os.getenv("api_key") or os.getenv("DASHSCOPE_API_KEY")


def _get_ocr_model() -> str:
    """读取 OCR 模型名。

    被谁调用：ocr_pdf()。返回：环境变量 OCR_MODEL，未配置时回退
    默认模型 qwen-vl-plus（成本与识别率的折中选择）。
    """
    return os.getenv("OCR_MODEL", "qwen-vl-plus")


def _encode_image_base64(image_path: str) -> str:
    """把单张图片文件读取并编码为 base64 字符串。

    被谁调用：_ocr_single_page()。参数 image_path 为
    pdf_to_images 产出的临时 PNG 路径。返回：可拼入
    data:image/png;base64, URL 的 base64 字符串。
    """
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


# tenacity 重试策略：任何异常最多重试 3 次，指数退避等待 2~10 秒，
# 最终仍失败则原样抛出（reraise=True），由 ocr_pdf 的单页降级逻辑接住
@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type(Exception),
    reraise=True,
)
def _ocr_single_page(image_path: str, api_key: str, model: str) -> str:
    """对单页 PNG 图片进行 OCR，返回识别文本。

    被谁调用：ocr_pdf() 逐页循环调用。
    参数：
    - image_path (str)：pdf_to_images 产出的临时 PNG 路径；
    - api_key (str)：DashScope API Key，来自环境变量；
    - model (str)：OCR 多模态模型名（默认 qwen-vl-plus）。
    返回：
    - str：该页识别文本（已 strip）。响应 content 为分段列表时
      拼接各段 text；兼容 output.text 等其他返回形态。
    异常：
    - HTTP 状态码非 200 时抛 RuntimeError，触发 tenacity 重试；
      3 次仍失败则向上抛出，由 ocr_pdf 记为该页空文本继续处理。
    """
    # 延迟导入：仅在真正走 OCR 路径时才加载 dashscope SDK
    from dashscope import MultiModalConversation

    img_b64 = _encode_image_base64(image_path)

    messages = [
        {
            "role": "user",
            "content": [
                {"image": f"data:image/png;base64,{img_b64}"},
                {"text": _OCR_PROMPT},
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
            f"OCR API returned {response.status_code}: {getattr(response, 'message', 'unknown error')}"
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
    # 兜底：未知响应结构时强转字符串，尽量不丢识别结果
    return str(output).strip()


def ocr_pdf(file_path: str) -> str:
    """扫描件 PDF → 逐页 OCR → 拼接全文（模块对外主入口）。

    被谁调用：app/application/files/file_service.py 的 _extract_text()，
    doc_type 判定为 "scanned" 时进入本路径
    （文件.函数：file_service._extract_text）；返回的原始文本随后
    交给 ocr_clean.clean_ocr_text 清洗。

    流程：
    1. PDF → PNG 图片列表（pdf_to_images，临时目录）
    2. 逐页调用 qwen-vl-plus OCR（单页失败重试 3 次）
    3. 拼接各页文本（页间用 \\n\\n 分隔）
    4. 无论成功失败都在 finally 中清理临时图片文件

    参数：
    - file_path (str)：已落盘扫描件 PDF 的本地路径，由 file_service
      传入（最初来自 control/file_control 保存的前端上传文件）。

    返回：
    - str：全书 OCR 文本；单页失败时该页以空串占位、不中断整体；
      PDF 无页可渲染时返回空串，由上层报“未解析到文本内容”。

    异常：
    - 未配置 API Key：抛 RuntimeError，上层 _extract_text 无额外
      捕获时归入上传失败降级；
    - PDF 转图失败：异常向上抛出（临时文件已在 pdf_to_images 内清理）。
    """
    api_key = _get_api_key()
    if not api_key:
        raise RuntimeError("DashScope API key not configured for OCR")

    model = _get_ocr_model()
    image_paths = pdf_to_images(file_path)

    if not image_paths:
        return ""

    page_texts = []
    try:
        for i, img_path in enumerate(image_paths):
            try:
                text = _ocr_single_page(img_path, api_key, model)
                page_texts.append(text)
                logger.info("OCR page %d/%d: %d chars", i + 1, len(image_paths), len(text))
            except Exception as e:
                # 单页降级：重试 3 次仍失败的页记 warning 并以空串占位，
                # 不拖垮整本 PDF 的 OCR 结果
                logger.warning("OCR failed for page %d of %s: %s", i + 1, file_path, e)
                page_texts.append("")
    finally:
        # 无条件清理临时 PNG 与临时目录，避免磁盘泄漏
        cleanup_images(image_paths)

    # 过滤完全为空的页后用空行拼接，空页不产生多余段落分隔
    return "\n\n".join(t for t in page_texts if t)
