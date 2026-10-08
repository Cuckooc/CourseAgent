"""
模块名：core.output_validator

作用：
    结构化输出验证器：Pydantic 模型定义 + 验证失败回传修正。

    第 2 层防线：生成后验证。配合 model_llm 的 json_mode（第 1 层生成时约束），
    对 LLM 返回的 JSON 做 Schema 校验；失败时将错误信息拼回 prompt 让模型修正（最多 1 次重试）。

主要成员：
    - SummaryOutput / DiagnosisOutput：Pydantic 输出 Schema 模型（约束字段类型与长度）；
    - validate_json_output(raw_text, output_model)：校验单段 JSON 文本；
    - validate_with_retry(raw_text, output_model, llm, original_prompt, max_retries)：
      校验失败时把错误回传 LLM 自我修正后重试。

被谁使用：
    - app/domain/agents/summary_agent.py：导入 validate_json_output 与 SummaryOutput，
      解析 SummaryAgent 的 JSON 输出（失败则宽松解析兜底）。
    - 说明（Grep 全仓确认）：DiagnosisOutput 为预留给 FailureDiagnoser 的输出模型，
      validate_with_retry 为通用重试入口，当前仓库内暂无调用方（core/param_validator.py
      中同名方法是另一处独立实现，与本函数无关）。
"""
import json
import logging
from typing import List

from pydantic import BaseModel, Field, ValidationError

logger = logging.getLogger(__name__)


# ==================== 输出 Schema 定义 ====================

class SummaryOutput(BaseModel):
    """SummaryAgent（InformationLLM）的结构化输出模型（Pydantic 校验用，不实例化入库）。

    用途：约束总结类 Agent 返回 JSON 必须含非空 summary（1~2000 字）与 keywords 列表；
    作为 output_model 传给 validate_json_output / validate_with_retry。
    使用位置：app/domain/agents/summary_agent.py。
    """
    summary: str = Field(..., min_length=1, max_length=2000, description="汇总后的完整总结")
    keywords: List[str] = Field(default_factory=list, description="核心关键词列表")


class DiagnosisOutput(BaseModel):
    """FailureDiagnoser 的结构化输出模型（Pydantic 校验用，不实例化入库）。

    用途：约束失败诊断 Agent 返回 JSON 的 type（MISSING_INFO/TECHNICAL_ERROR）、
    reason 与澄清问题 clarification；当前为预留模型，仓库内暂无调用方。
    """
    type: str = Field(..., description="MISSING_INFO 或 TECHNICAL_ERROR")
    reason: str = Field(default="", description="一句话解释失败原因")
    clarification: str = Field(default="", description="仅 MISSING_INFO 时填写澄清问题")


# ==================== 验证 + 重试 ====================

def validate_json_output(
    raw_text,       # type: str
    output_model,   # type: Type[BaseModel]
):
    # type: (...) -> Tuple[Optional[BaseModel], str]
    """验证 JSON 文本是否符合 Schema。

    功能：剥离 Markdown 代码块包裹 → json 解析（必须是 dict）→ Pydantic v2
    model_validate 校验；环境为 Pydantic v1（无 model_validate）时回退构造方式。
    被谁调用：app/domain/agents/summary_agent.py 解析 SummaryAgent 输出；
              本模块 validate_with_retry 内部每轮也调用。
    参数：
        raw_text: LLM 原始返回文本，来源为上游 multi_agent 调 LLM 的响应内容；
        output_model: Pydantic 模型类（Type[BaseModel]），如 SummaryOutput，调用方指定。
    返回：
        Tuple[Optional[BaseModel], str]：(parsed_model, error_message)。
        成功为 (模型实例, "")；parsed_model 为 None 表示验证失败，
        error_message 描述具体原因（JSON 解析失败/类型不符/Schema 错误，均截断限长）。
    """
    text = raw_text.strip()
    # 剥离 Markdown 代码块包裹（模型常返回 ```json ... ```）
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:]) if len(lines) > 1 else text
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, "JSON 解析失败: {}".format(str(e)[:200])

    if not isinstance(data, dict):
        return None, "期望 JSON 对象，实际类型为: {}".format(type(data).__name__)

    try:
        model = output_model.model_validate(data)
        return model, ""
    except ValidationError as e:
        errors = []
        for err in e.errors():
            loc = ".".join(str(loc) for loc in err["loc"])
            errors.append("{}: {}".format(loc, err["msg"]))
        return None, "Schema 验证失败: {}".format("; ".join(errors)[:300])
    except AttributeError:
        # Pydantic v1 兼容
        try:
            model = output_model(**data)
            return model, ""
        except ValidationError as e:
            errors = []
            for err in e.errors():
                loc = ".".join(str(loc) for loc in err["loc"])
                errors.append("{}: {}".format(loc, err["msg"]))
            return None, "Schema 验证失败: {}".format("; ".join(errors)[:300])


def validate_with_retry(
    raw_text,        # type: str
    output_model,    # type: Type[BaseModel]
    llm,             # type: Any
    original_prompt, # type: str
    max_retries=1,   # type: int
):
    # type: (...) -> Tuple[Optional[BaseModel], str]
    """验证 JSON 输出，失败时将错误回传给 LLM 修正（最多 max_retries 次）。

    功能：先校验一次；失败则把校验错误拼接到原 prompt（截断 1500 字）后调用
    llm.invoke 要求模型只返回合法 JSON，再校验，直至通过或重试耗尽。
    被谁调用：通用修正入口（Grep 全仓当前无业务调用方；SummaryAgent 现直接用
              validate_json_output + 宽松解析兜底）。
    参数：
        raw_text: LLM 首次返回的原始文本，来源为上游 Agent 的 LLM 响应；
        output_model: 期望输出的 Pydantic 模型类（Type[BaseModel]）；
        llm: 具备 invoke(prompt)->response（response.content 为文本）的 LLM 客户端，
             来源为 model_llm 网关构建；
        original_prompt: 首次发给模型的原始 prompt，用于拼接修正提示；
        max_retries: 最大修正重试次数，默认 1。
    返回：
        Tuple[Optional[BaseModel], str]：(parsed_model, final_raw_text)。
        成功时 parsed_model 为模型实例；重试用尽仍失败时为 (None, 最后一次原始文本)，
        由调用方决定兜底策略。
    """
    model, error = validate_json_output(raw_text, output_model)
    if model is not None:
        return model, raw_text

    for attempt in range(1, max_retries + 1):
        logger.warning(
            "Output validation failed (attempt %d/%d): %s",
            attempt, max_retries, error,
        )
        correction_prompt = (
            "{original}\n\n"
            "你上一次返回的 JSON 格式有误：{error}\n"
            "请修正后重新输出，仅返回合法的 JSON 字符串，不要包含其他内容："
        ).format(original=original_prompt[:1500], error=error)

        try:
            response = llm.invoke(correction_prompt)
            raw_text = response.content if hasattr(response, "content") else str(response)
            model, error = validate_json_output(raw_text, output_model)
            if model is not None:
                logger.info("Output validation passed on retry %d", attempt)
                return model, raw_text
        except Exception as e:
            logger.warning("Retry LLM correction failed: %s", e)
            break

    logger.error("Output validation failed after %d retries: %s", max_retries, error)
    return None, raw_text
