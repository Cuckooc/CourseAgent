/**
 * @文件 api/sessionApi.js
 * @作用 会话/历史管理 API：会话列表、历史信息分页、会话详情、创建、重命名、删除（两步确认）。
 *       全部走 services/http.js 统一封装（token 注入 / 401 拦截跳登录 / status=fail 抛 ApiError）。
 *       后端前缀 /history（control/history_control.py 的 history_router），路由级限流 60 次/分钟。
 *       注意：后端会话域挂在 /history 前缀下，前端语义上称「会话（session）」。
 * @主要成员 fetchSessionList、fetchHistoryPage、fetchSessionDetail、createSession、
 *   updateSessionTitle、deleteSession
 * @被谁使用 stores/sessionStore.js（列表/创建/重命名/删除）、stores/chatStore.js（fetchSessionDetail
 *   加载聊天记录）、pages/HistoryPage.jsx（fetchHistoryPage 历史信息分页）。
 */
import { post } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchSessionList
 * @description 查询当前用户的会话列表（聊天侧栏用，侧栏无分页控件，需一次取满）。
 *   对应后端接口：POST /history/list?page=1&page_size=50（history_router），请求体 {}。
 *   后端 page_size 硬上限为 50（Query(le=50)），缺省值仅 10——不显式传参会导致
 *   会话超过 10 个的用户侧栏只显示最近 10 条，故固定传上限 50。
 * @returns {Promise<Array<{ session_id: number, title: string }>>}
 *   会话简要列表（body.data 兜底 []，最多 50 条，按最后消息时间倒序）；
 *   交由 sessionStore.fetchList 更新侧栏 sessions。
 * @throws {ApiError} 401 跳登录；429 等错误由调用方 catch 处理。
 */
export function fetchSessionList() {
  return post(`${ENDPOINTS.HISTORY_LIST}?page=1&page_size=50`, {}).then((body) => body.data || []);
}

/**
 * @function fetchHistoryPage
 * @description 历史信息页分页查询（长期记忆滑动窗口，每页 10 个会话），支持时间范围过滤。
 *   对应后端接口：POST /history/list?range=&page=&page_size=（query 传分页，body 为空 {}）。
 * @param {object} [params]
 * @param {'day'|'week'|'all'} [params.range='all'] 时间范围（来自历史页范围切换，URL 编码拼参）。
 * @param {number} [params.page=1] 页码（来自分页器）。
 * @param {number} [params.pageSize=10] 每页条数。
 * @returns {Promise<{ data: Array, total: number, page: number, page_size: number, hasMore?: boolean }>}
 *   后端完整响应体（含分页字段）；交由 HistoryPage 渲染时间线与翻页。
 * @throws {ApiError} 401 跳登录；429 等错误由页面 catch 提示。
 */
export function fetchHistoryPage({ range = 'all', page = 1, pageSize = 10 } = {}) {
  const qs = `range=${encodeURIComponent(range)}&page=${page}&page_size=${pageSize}`;
  return post(`${ENDPOINTS.HISTORY_LIST}?${qs}`, {});
}

/**
 * @function fetchSessionDetail
 * @description 查询单个会话的聊天记录（切换会话/刷新恢复时由 chatStore.loadHistory 调用）。
 *   对应后端接口：POST /history/detail，请求体 { session_id }。
 * @param {number} sessionId 会话 ID（来自 sessionStore.currentSessionId）。
 * @returns {Promise<Array<{ session_id: number, content: string, type: 'user'|'assistant', role?: string }>>}
 *   消息行数组（body.data 兜底 []，chatStore 兼容 role/type 两种字段）；越权访问他人会话时后端返回空数组。
 * @throws {ApiError} 401 跳登录；其他错误由 chatStore.loadHistory 的 finally 复位 loading（错误向上传播）。
 */
export function fetchSessionDetail(sessionId) {
  return post(ENDPOINTS.HISTORY_DETAIL, { session_id: sessionId }).then((body) => body.data || []);
}

/**
 * @function createSession
 * @description 创建新会话（标题可缺省，由后端定为「新会话」）。
 *   对应后端接口：POST /history/create，请求体 {} 或 { title }。
 * @param {string} [title] 会话标题（来自新建会话命名输入；为空时不传 title）。
 * @returns {Promise<{ session_id: number, title: string }>} 新会话 ID 与标题；
 *   交由 sessionStore.create 置顶并设为当前会话、持久化到 sessionStorage。
 * @throws {ApiError} 401/429 等，由调用方（侧栏/页面）提示。
 */
export function createSession(title) {
  return post(ENDPOINTS.HISTORY_CREATE, title ? { title } : {}).then((body) => ({
    session_id: body.session_id,
    title: body.title,
  }));
}

/**
 * @function updateSessionTitle
 * @description 重命名会话标题。
 *   对应后端接口：POST /history/update_title，请求体 { session_id, title }。
 * @param {number} sessionId 会话 ID（来自侧栏重命名的目标行）。
 * @param {string} title 新标题（来自内联编辑输入框）。
 * @returns {Promise<{ message: string }>} 成功返回 { message:'更新成功' }；
 *   sessionStore.rename 成功后同步本地列表。
 * @throws {ApiError} 越权/不存在时后端返回 fail，http.js 抛 ApiError('更新失败' 或响应文案)。
 */
export function updateSessionTitle(sessionId, title) {
  return post(ENDPOINTS.HISTORY_UPDATE_TITLE, { session_id: sessionId, title });
}

/**
 * @function undoLastDelete
 * @description 撤销当前用户最近一次软删除（后端每用户一次性机会：UndoStore 快照消费后失效）。
 *   对应后端接口：POST /history/undo_delete（history_router），无请求体。
 *   恢复顺序：最近删除的会话（history_information + session_information 一并恢复），
 *   其次用户画像。
 * @returns {Promise<{status:string, message:string, type:'session'|'profile'}>}
 *   type 标识实际恢复的对象类型；恢复后调用方应重新拉取会话列表。
 * @throws {ApiError} 404 没有可撤销的删除记录；500 恢复失败（记录可能已被彻底清理）。
 */
export function undoLastDelete() {
  return post(ENDPOINTS.HISTORY_UNDO_DELETE, {});
}

/**
 * @function deleteSession
 * @description 删除会话（两步确认：preview 取 confirm_token → confirm 执行）；
 *   后端同时清理该会话的临时知识库。
 *   对应后端接口：POST /history/delete/preview → POST /history/delete/confirm（history_router）。
 * @param {number} sessionId 待删除会话 ID（来自侧栏删除操作，通常带 Popconfirm）。
 * @returns {Promise<object>} confirm 步骤完整响应体（含 message 等）；
 *   交由 sessionStore.remove 从本地列表移除，若删的是当前会话则清空当前指向。
 * @throws {ApiError} 令牌失效/越权/401/429 等，由调用方提示且本地列表不变。
 */
export async function deleteSession(sessionId) {
  const preview = await post(ENDPOINTS.HISTORY_DELETE_PREVIEW, { session_id: sessionId });
  return post(ENDPOINTS.HISTORY_DELETE_CONFIRM, {
    session_id: sessionId,
    confirm_token: preview.confirm_token,
  });
}
