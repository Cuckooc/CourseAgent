/**
 * @文件 api/profileApi.js
 * @作用 用户画像（长期记忆中的用户习惯/兴趣/常问主题）读取与修改 API。
 *       全部请求经 services/http.js 统一通道（Bearer token 注入、401 全局登出、status=fail 抛错）。
 *       后端前缀 /profile（control/profile_control.py 的 profile_router），路由级限流 60 次/分钟。
 *       读取：MySQL 基线 + Redis 暂存（7 天内有变更时 pending=true）。
 *       修改：仅写 Redis 暂存并重置 7 天计时，连续 7 天无更新才落 MySQL。
 * @主要成员 fetchProfile、updateProfile
 * @被谁使用 pages/ProfilePage.jsx（个人信息页表单初始化加载与保存按钮提交；页面路由为 /account）。
 */
import { get, put } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchProfile
 * @description 读取当前登录用户的画像。
 *   对应后端接口：GET /profile（profile_router，后端同时注册 '' 与 '/' 两条路径）。
 * @returns {Promise<{ user_id: number, profile_text: string, interests: string, topics: string, pending: boolean, due_at: number|null }>}
 *   画像数据（取 body.data，无 data 时空对象兜底）；交由 ProfilePage 回填编辑表单，
 *   pending=true 时页面提示「有暂存修改尚未落库」。
 * @throws {ApiError} 401 跳登录；429 等错误由页面 catch 提示。
 */
export function fetchProfile() {
  return get(ENDPOINTS.PROFILE).then((body) => body.data || {});
}

/**
 * @function updateProfile
 * @description 手动修改画像（整表提交：前端编辑表单始终回传全部字段；后端仅暂存 Redis，7 天计时重置）。
 *   对应后端接口：PUT /profile（profile_router），请求体 { profile_text, interests, topics }。
 * @param {object} [params]
 * @param {string} [params.profile_text=''] 画像描述文本（来自 ProfilePage 表单）。
 * @param {string} [params.interests=''] 兴趣字段（来自表单）。
 * @param {string} [params.topics=''] 常问主题字段（来自表单）。
 * @returns {Promise<object>} 更新后的画像（body.data，pending=true）；交由页面展示保存成功与暂存状态。
 * @throws {ApiError} 401 跳登录；字段超长等业务 fail 由 http.js 抛出，页面 message.error 提示。
 */
export function updateProfile({ profile_text = '', interests = '', topics = '' } = {}) {
  return put(ENDPOINTS.PROFILE, { profile_text, interests, topics }).then((body) => body.data || {});
}
