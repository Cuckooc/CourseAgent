"""
模块：profile_control.py
作用：用户画像（用户习惯/兴趣/常问主题）HTTP 接口，提供画像查询与手动修改。
主要成员：
- profile_router：用户画像路由对象（prefix=/profile，路由级登录用户限流）；
- ProfileUpdateRequest：画像修改请求体模型；
- get_profile：获取当前用户画像（GET /profile 与 /profile/ 双装饰器）；
- update_profile：手动修改画像（PUT /profile 与 /profile/ 双装饰器）。
被谁使用：由 control/app.py 通过 `from control.profile_control import profile_router`
          导入并 app.include_router 注册；JWT 鉴权，user_id 取自登录态；
          路由由 HTTP 客户端（web/frontend 个人信息页）调用，非内部调用。

读取：MySQL 基线 + Redis 暂存（7 天内有变更时以暂存为准）。
修改：写入 Redis 暂存并重新计时，连续 7 天无更新才由后台任务落 MySQL。
"""
from typing import Optional

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from app.auth.guards import get_current_user
from app.auth.rate_limit import user_rate_limit
from app.domain.memory.profile_service import get_profile_service

# 用户画像路由：prefix=/profile，由 control/app.py 的 app.include_router(profile_router) 注册。
# 路由级依赖 user_rate_limit(60, 60)：按登录用户限流 60 次/分钟。
profile_router = APIRouter(
    prefix="/profile", tags=["profile control"],
    dependencies=[Depends(user_rate_limit(60, 60))],
)


class ProfileUpdateRequest(BaseModel):
    """画像修改请求体模型。

    实例化位置：由前端个人信息页提交的 JSON 请求体经 FastAPI/Pydantic 自动校验实例化，
    作为 update_profile 路由的 req 参数。
    字段（均为可选，缺省为空串）：
    - profile_text：画像自由文本（用户习惯/整体描述），来源前端编辑框；最长 4000 字符；
    - interests：兴趣标签/描述，来源前端；最长 500 字符；
    - topics：常问主题，来源前端；最长 500 字符。
    """

    profile_text: Optional[str] = Field(default="", max_length=4000)
    interests: Optional[str] = Field(default="", max_length=500)
    topics: Optional[str] = Field(default="", max_length=500)


@profile_router.get("")
@profile_router.get("/")
def get_profile(current_user: dict = Depends(get_current_user)):
    """获取当前用户画像（个人信息页展示）。

    HTTP 方法+路径：GET /profile 与 GET /profile/（双装饰器兼容带斜杠写法）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 个人信息页）调用，非内部调用。
    参数：
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应 {status:"success", data: profile}；
          profile 由 MySQL 基线与 Redis 暂存合并而成，pending=true 表示存在未落库的近期修改。
    """
    service = get_profile_service()
    profile = service.get_profile(current_user["user_id"])
    return {"status": "success", "data": profile}


@profile_router.put("")
@profile_router.put("/")
def update_profile(req: ProfileUpdateRequest, current_user: dict = Depends(get_current_user)):
    """
    手动修改画像：更新仅暂存 Redis 并重置 7 天计时；
    连续 7 天没有再次更新才落 MySQL。未提交修改不影响原画像。

    HTTP 方法+路径：PUT /profile 与 PUT /profile/（双装饰器兼容带斜杠写法）。
    鉴权与限流：get_current_user 依赖做 JWT 鉴权并注入当前用户；路由级 60 次/分钟限流。
    被谁调用：由 HTTP 客户端（web/frontend 个人信息页保存按钮）调用，非内部调用。
    参数：
    - req：画像修改请求体，ProfileUpdateRequest 模型，三个文本字段均来自前端编辑框；
    - current_user：Depends(get_current_user) 注入的 JWT 用户信息。
    返回：HTTP JSON 响应 {status:"success", data: profile, message:"画像已更新"}，
          data 为更新后的画像（暂存态）。
    """
    service = get_profile_service()
    profile = service.update_profile(
        current_user["user_id"],
        profile_text=req.profile_text or "",
        interests=req.interests or "",
        topics=req.topics or "",
    )
    return {"status": "success", "data": profile, "message": "画像已更新"}
