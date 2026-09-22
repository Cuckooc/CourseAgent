/**
 * @文件 api/adminApi.js
 * @作用 管理端用户管理 API 封装：全部用户列表查询、用户角色调整、注销用户账户。
 *       全部请求经 services/http.js 统一通道（自动注入 Authorization: Bearer token、
 *       401 全局登出跳转、429/4xx/5xx 抛 ApiError）。
 *       后端前缀 /admin（control/admin_control.py 的 admin_router），路由级限流 30 次/分钟，
 *       且仅 admin 角色可访问（普通用户 403，由前端路由守卫 RequirePermission 先行拦截）。
 * @主要成员 fetchAdminUsers、updateAdminUserRole、deactivateAdminUser
 * @被谁使用 pages/AdminUsersPage.jsx（用户管理表格的加载、角色下拉调整、Popconfirm 注销按钮）。
 */
import { get, post, put } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchAdminUsers
 * @description 查询全部用户基础信息（管理端用户表格首屏加载与操作后刷新使用）。
 * 对应后端接口：GET /admin/users（control/admin_control.py admin_router）。
 * @returns {Promise<{ users: Array<{user_id?:number, id:number, user_name:string, email:string, role:string}> }>}
 *   完整响应体；页面取 body.users 并以 id 作为表格行 key（兼容 user_id 字段）。
 * @throws {ApiError} 401 登录失效（http.js 全局跳登录）；403 非 admin 角色；429 触发限流提示。
 */
export function fetchAdminUsers() {
  return get(ENDPOINTS.ADMIN_USERS);
}

/**
 * @function updateAdminUserRole
 * @description 调整指定用户的角色（user ⇄ teacher，即时生效；admin 角色不在前端调整范围内）。
 * 对应后端接口：PUT /admin/users/{user_id}/role，请求体 { role }，
 * 后端成功后会自增该用户 token 版本，使其旧令牌失效（需重新登录）。
 * @param {number} userId 目标用户 ID（来自用户表格行 id，由管理员在角色下拉框触发）。
 * @param {'user'|'teacher'} role 新角色（来自 AdminUsersPage 角色 Select 的选项）。
 * @returns {Promise<object>} 完整响应体（含 message 等平铺字段），页面用 message 弹出成功提示。
 * @throws {ApiError} 越权/参数非法/429 时抛出，页面 catch 后 message.error 提示。
 */
export function updateAdminUserRole(userId, role) {
  return put(`${ENDPOINTS.ADMIN_USERS}/${userId}/role`, { role });
}

/**
 * @function deactivateAdminUser
 * @description 注销指定用户账户（两步确认）：先 POST preview 取一次性确认令牌，
 *   再 POST confirm 携令牌执行注销。后端执行 MySQL 软删除 + 注销计划 + Redis 清理，
 *   级联处理其会话/历史/画像，其 token 立即失效。
 *   对应后端接口（control/admin_control.py admin_router）：
 *   1. POST /admin/users/{user_id}/deactivate/preview（无请求体，路径参数定位用户）
 *      → 返回 { confirm_token, target_user_id, target_user_name }；
 *   2. POST /admin/users/{user_id}/deactivate/confirm，请求体 { confirm_token }
 *      → 返回 { message }。
 *   与 knowledgeApi.deleteKnowledgeFile / sessionApi 的删除链路同为 preview→confirm 模式。
 * @param {number} userId 目标用户 ID（来自用户表格行 id，Popconfirm 二次确认后触发）。
 * @returns {Promise<{ message?: string }>} confirm 接口完整响应体，页面取 message 作为成功提示文案。
 * @throws {ApiError} preview 阶段 400（注销自己）/403（目标为 admin）/404（用户不存在）、
 *   confirm 阶段令牌无效或过期（400）、401/429 等；页面 catch 后展示 e.message。
 */
export async function deactivateAdminUser(userId) {
  // 第一步：预览校验（不能注销自己/admin/不存在）并签发一次性 confirm_token，仅预览不执行注销
  const preview = await post(`${ENDPOINTS.ADMIN_USERS}/${userId}/deactivate/preview`);
  // 第二步：原样回传 confirm_token，校验通过后后端执行软删除与级联清理
  return post(`${ENDPOINTS.ADMIN_USERS}/${userId}/deactivate/confirm`, {
    confirm_token: preview.confirm_token,
  });
}
