/**
 * @文件 api/usageApi.js
 * @作用 LLM 用量统计 API：按模型聚合的全局用量快照、按用户聚合的月度用量快照。
 *       全部请求经 services/http.js 统一通道（Bearer token 注入、401 全局登出、status=fail 抛错）。
 *       后端前缀 /admin（control/admin_control.py 的 admin_router），路由级限流 30 次/分钟，
 *       仅 admin 角色可访问；普通用户调用返回 HTTP 403（http.js 抛 ApiError），
 *       由路由守卫 RequirePermission 在前端先行拦截。
 * @主要成员 fetchLlmUsage、fetchUserUsage
 * @被谁使用 stores/usageStore.js（用量看板状态），最终由 pages/UsagePage.jsx 订阅渲染。
 */
import { get } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchLlmUsage
 * @description 查询按模型聚合的 LLM 用量快照（请求数与各类 token 数）。
 *   对应后端接口：GET /admin/llm/usage（admin_router）。
 * @returns {Promise<Object<string, { requests: number, prompt_tokens: number, completion_tokens: number }>>}
 *   以模型名为键的用量映射（取 body.usage，缺省 {}）；交由 usageStore.fetchUsage 供 UsagePage 模型维度表格展示。
 * @throws {ApiError} 401 跳登录；403 非 admin；429 触发限流提示。
 */
export function fetchLlmUsage() {
  return get(ENDPOINTS.ADMIN_LLM_USAGE).then((body) => body.usage || {});
}

/**
 * @function fetchUserUsage
 * @description 查询按用户聚合的指定月度用量快照。
 *   对应后端接口：GET /admin/llm/usage/users（不带 month 时取后端默认当前月）或
 *   GET /admin/llm/usage/users?month=YYYY-MM。
 * @param {string} [month] 月份字符串 YYYY-MM（来自 UsagePage 月份选择器；缺省由后端取当前月）。
 * @returns {Promise<{ month: string, rows: Array<{user_id:number, user_name:string, email:string, role:string, requests:number, prompt_tokens:number, completion_tokens:number}> }>}
 *   月份与每用户用量行（rows 缺省 []）；交由 usageStore.fetchUserUsage 供 UsagePage 用户维度表格展示。
 * @throws {ApiError} 401 跳登录；403 非 admin；429 触发限流提示。
 */
export function fetchUserUsage(month) {
  const url = month
    ? `${ENDPOINTS.ADMIN_LLM_USAGE_USERS}?month=${encodeURIComponent(month)}`
    : ENDPOINTS.ADMIN_LLM_USAGE_USERS;
  return get(url).then((body) => ({
    month: body.month,
    rows: body.rows || [],
  }));
}

