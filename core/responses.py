"""
模块名：core.responses（统一响应体与业务异常）。

作用：
    定义全仓统一的 HTTP 响应 JSON 结构，以及可在任意层抛出的业务异常，
    保证前端收到的成功/失败报文契约一致。

统一响应格式：
    成功: {"status": "success", "code": 0, "message": "success", ...业务字段}
    失败: {"status": "fail", "code": <http_status>, "message": "..."}
    （异常处理器输出的失败报文还会额外携带 request_id 字段，见 control/app.py）

主要成员：
    - BizException：业务异常类，由 control/app.py 的全局异常处理器捕获并
      转换为统一失败响应；
    - success()：构造成功响应体（业务字段平铺顶层）；
    - fail()：构造失败响应体。

被谁使用（Grep）：
    - BizException：core.deps、core.security 及 control 下各路由层
      （admin/chat/file/knowledge/history/review 等）广泛抛出，
      control/app.py 注册 @app.exception_handler(BizException) 统一兜底；
    - success：control/admin_control.py、knowledge_control.py、
      login_control.py、review_control.py 的端点返回；
    - fail：全仓 Grep 暂无调用方（作为与 success 对称的工具函数保留，
      当前失败路径统一走 BizException）。
"""
from typing import Any, Dict, Optional


class BizException(Exception):
    """业务异常：由全局异常处理器转换为统一失败响应。

    作用：在 service/dao/deps 等非 Web 层表达“可预期的业务失败”，
    携带 HTTP 状态码与提示信息一路抛到路由外。
    实例化位置（raise 处）：core.deps（鉴权/限流 401/403/429）、
    core.security（token 解析失败 401）、control 下各路由的参数/权限校验；
    捕获位置：control/app.py 的 biz_exception_handler，
    转为 {"status":"fail","code":...,"message":...,"request_id":...} 响应体。
    关键属性去向：message 进入响应 message 字段；http_status 作为 HTTP
    状态码；code 缺省取 http_status，进入响应 code 字段。
    """

    def __init__(self, message: str, http_status: int = 400, code: Optional[int] = None):
        # message: 面向用户的中文失败提示，来源为各抛出点的校验信息
        # http_status: 对应 HTTP 状态码（如 400/401/403/404/429）
        # code: 响应体业务码；未显式给定时与 http_status 相同
        self.message = message
        self.http_status = http_status
        self.code = code if code is not None else http_status
        super().__init__(message)


def success(**data: Any) -> Dict[str, Any]:
    """构造成功响应体（业务字段直接平铺在顶层，兼容现有前端契约）。

    被谁调用：control/admin_control.py、knowledge_control.py、
        login_control.py、review_control.py 等端点成功返回时。
    参数：
        **data: 任意业务字段（如 token、usage、列表数据），来源为各
        service 的处理结果，与基础字段平铺合并到同一 JSON 顶层。
    返回：Dict[str, Any]，形如
        {"status":"success","code":0,"message":"success", **data}，
        去向为 FastAPI 端点返回值（自动序列化为 JSON 响应）。
    """
    body: Dict[str, Any] = {"status": "success", "code": 0, "message": "success"}
    body.update(data)
    return body


def fail(message: str, http_status: int = 400) -> Dict[str, Any]:
    """构造失败响应体。

    被谁调用：全仓 Grep 当前无调用方（保留的对称工具函数；线上失败路径
        统一通过 raise BizException 由全局处理器生成失败报文）。
    参数：
        message: 失败提示，来源为调用方给出的业务错误信息；
        http_status: 失败码，默认 400，进入响应体 code 字段。
    返回：Dict[str, Any]，形如
        {"status":"fail","code":http_status,"message":message}。
    """
    return {"status": "fail", "code": http_status, "message": message}
