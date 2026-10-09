"""
模块名：core.param_validator（参数校验器）。

作用：
    为 LLM/工具调用提取出的参数提供 Pydantic schema 声明、运行时类型校验，
    以及“校验失败 → 带错误信息重新让 LLM 提取”的有限次重试包装。
    校验通过即放行；重试次数耗尽仍不通过时由调用方走 fallback 降级。

主要成员：
    - ToolParamSchema：单个工具参数的 schema 声明（名称/类型/描述/是否必填）；
    - _TYPE_MAP：schema 类型字符串到 Python 原生类型的映射表；
    - ParamValidator：纯静态工具类，提供 validate() 与 validate_with_retry()。

被谁使用：
    全仓 Grep（param_validator / ParamValidator / ToolParamSchema）当前未发现
    外部 import，属于为工具调用参数校验链路预留的通用组件；模块内
    ParamValidator.validate_with_retry 会调用 ParamValidator.validate。
    注意 core.output_validator 中同名的 validate_with_retry 是另一个独立函数，
    与本模块无调用关系。
"""
import logging

from pydantic import BaseModel, field_validator

logger = logging.getLogger(__name__)


class ToolParamSchema(BaseModel):
    """单个工具参数的 schema 声明（Pydantic 模型）。

    作用：描述一个工具形参的名称、类型、含义与是否必填，作为 ParamValidator
    遍历校验时的元数据来源；字段定义本身即校验规则（Pydantic 在实例化时校验）。
    实例化位置：全仓 Grep 未发现显式构造点，当前为预留声明结构，
    预期由工具注册表按工具定义批量构造后传入 ParamValidator。
    关键属性去向：name/type/required 在 ParamValidator.validate 中被读取，
    description 用于拼装给 LLM 的工具说明。
    """

    name: str
    # 参数类型字符串，仅允许 _TYPE_MAP 支持的 6 种（见 validate_type）
    type: str
    # 参数自然语言描述，随工具定义提供给 LLM
    description: str
    # 是否必填；False 时参数缺失直接放行
    required: bool = True

    @field_validator("type")
    @classmethod
    def validate_type(cls, v):
        # Pydantic 字段校验器：把 type 限定在受支持的类型集合内，
        # 防止 schema 配置了校验器无法识别的类型
        allowed = {"str", "int", "float", "bool", "list", "dict"}
        if v not in allowed:
            raise ValueError(
                "type must be one of {}, got '{}'".format(allowed, v)
            )
        return v


# schema 类型字符串 -> Python 原生类型的映射；
# float 同时接受 int（JSON 中整数可作为浮点参数）
_TYPE_MAP = {
    "str": str,
    "int": int,
    "float": (int, float),
    "bool": bool,
    "list": list,
    "dict": dict,
}


class ParamValidator:
    """参数校验 + 重试包装（纯静态工具类，无需实例化，全部方法为 @staticmethod）。

    实例化位置：无；调用方以类名直接调用（ParamValidator.validate /
    ParamValidator.validate_with_retry）。当前全仓 Grep 仅见模块内自调用，
    外部工具调用链路接入后由工具执行器调用。
    """

    @staticmethod
    def validate(params, schema):
        # type: (Dict[str, Any], List[ToolParamSchema]) -> Tuple[bool, str]
        """校验一次 LLM 提取出的参数字典是否符合 schema。

        功能：逐项检查必填性、原生类型，以及字符串非空/长度上限（4000）。
        被谁调用：ParamValidator.validate_with_retry（core.param_validator）。
        参数：
            params: LLM 提取结果字典，来源为大模型工具调用解析出的 JSON；
            schema: ToolParamSchema 列表，来源为工具注册表中的参数定义。
        返回：(is_valid, error_message)，Tuple[bool, str]；
            校验失败时 error_message 为中文原因，会作为反馈喂给 extract_fn
            让 LLM 带着错误信息重新提取。
        """
        for field in schema:
            value = params.get(field.name)

            # 必填缺失直接判失败；非必填缺失则跳过该项
            if value is None:
                if field.required:
                    return False, "缺少必填参数: {}".format(field.name)
                continue

            # schema 未登记的类型不做类型判定（放行，交由后续执行环节处理）
            expected = _TYPE_MAP.get(field.type)
            if expected is None:
                continue

            if not isinstance(value, expected):
                return False, "参数 '{}' 类型错误: 期望 {}, 实际 {}".format(
                    field.name,
                    field.type,
                    type(value).__name__,
                )

            # 字符串专项：禁止空串、限制 4000 字符，防止异常长参数拖垮下游
            if field.type == "str" and isinstance(value, str):
                if len(value) == 0:
                    return False, "参数 '{}' 不能为空字符串".format(field.name)
                if len(value) > 4000:
                    return False, "参数 '{}' 长度超过上限 4000".format(field.name)

        return True, ""

    @staticmethod
    def validate_with_retry(
        params,
        schema,
        extract_fn,
        max_retries=3,
    ):
        # type: (Dict[str, Any], List[ToolParamSchema], Callable, int) -> Tuple[Dict[str, Any], bool]
        """校验失败时带错误信息重新提取，最多重试 max_retries 次。

        功能：先校验首轮参数；不通过则调用 extract_fn(error, attempt) 让 LLM
        重新提取并再次校验，直到通过或重试耗尽。
        被谁调用：当前全仓 Grep 无外部调用方，为工具执行链路预留；
        注意 core.output_validator.validate_with_retry 是同名但独立的函数。
        参数：
            params: 首轮 LLM 提取的参数字典；
            schema: ToolParamSchema 列表（工具注册表定义）；
            extract_fn: 重新提取回调，入参为 (上一轮错误信息, 当前第几轮)，
                返回新的参数字典，由 LLM 网关侧提供；
            max_retries: 最大重试次数，默认 3。
        返回：(final_params, success)，Tuple[Dict[str, Any], bool]；
            success=True 时 final_params 为首个通过校验的参数；
            success=False 表示重试耗尽，final_params 为首轮原始参数，
            由调用方记录审计并执行 fallback。
        """
        # 首轮快速通过：无需进入重试循环
        is_valid, error = ParamValidator.validate(params, schema)
        if is_valid:
            return params, True

        for attempt in range(1, max_retries + 1):
            logger.warning(
                "参数校验失败 (attempt %d/%d): %s", attempt, max_retries, error
            )
            try:
                # 把具体错误信息回喂模型，引导其修正参数
                new_params = extract_fn(error, attempt)
            except Exception as e:
                # 重新提取本身异常（模型/网络问题）不计入成功，继续下一轮
                logger.warning("重新提取参数异常: %s", e)
                continue

            is_valid, error = ParamValidator.validate(new_params, schema)
            if is_valid:
                return new_params, True

        # 全部重试耗尽：记录错误日志，交回首轮参数与失败标记，由调用方降级
        logger.error(
            "参数校验 %d 次重试全部失败，最后错误: %s", max_retries, error
        )
        return params, False
