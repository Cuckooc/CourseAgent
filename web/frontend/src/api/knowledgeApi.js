/**
 * @文件 api/knowledgeApi.js
 * @作用 知识库管理 API：文档列表查询、文档删除（preview/confirm 两步确认）。
 *       全部请求经 services/http.js 统一通道（Bearer token 注入、401 全局登出、status=fail 抛错）。
 *       后端前缀 /knowledge（control/knowledge_control.py 的 knowledge_router），
 *       路由级限流 30 次/分钟，删除端点额外 10 次/分钟。
 *       知识库分三类：public（公共）/ private（用户私有）/ temp（会话临时）；列表仅返回文件名与范围，不返回分块细节。
 * @主要成员 fetchKnowledgeList、deleteKnowledgeFile
 * @被谁使用 pages/KnowledgePage.jsx（表格初始化加载、删除按钮二次确认后的删除流程）。
 */
import { get, post } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchKnowledgeList
 * @description 查询当前用户可见的知识库文档列表。
 *   对应后端接口：GET /knowledge/list（knowledge_router），走 http.js GET 通道。
 * @returns {Promise<{ total: number, data: Array<{filename:string, scope:string, chunks:number, total_length:number, orphan:boolean}> }>}
 *   归一化后的列表结果；交由 KnowledgePage 渲染文档表格（total/data 缺省兜底 0/[]）。
 * @throws {ApiError} 401 跳登录；429 限流提示；其他错误由页面 catch 静默/提示。
 */
export function fetchKnowledgeList() {
  return get(ENDPOINTS.KNOWLEDGE_LIST).then((body) => ({
    total: body.total || 0,
    data: body.data || [],
  }));
}

/**
 * @function deleteKnowledgeFile
 * @description 删除知识库文档（两步确认）：先 POST preview 取确认令牌，再 POST confirm 执行删除；
 *   后端同时删除向量分块与 uploads 物理文件。
 *   对应后端接口：POST /knowledge/delete/preview → POST /knowledge/delete/confirm（knowledge_router）。
 * @param {string} stored_name 存储文件名（uid_名字_hash32.ext 或历史 hash32.ext，来自列表行/删除弹窗）。
 * @returns {Promise<{ deleted_chunks: number, file_removed: boolean }>}
 *   删除的向量分块数与物理文件是否移除；交由 KnowledgePage 弹出结果提示并刷新列表。
 * @throws {ApiError} preview 阶段文件不存在/无权限（404）、confirm 令牌失效、401/429 等，页面 catch 提示。
 */
export async function deleteKnowledgeFile(stored_name) {
  const preview = await post(ENDPOINTS.KNOWLEDGE_DELETE_PREVIEW, { stored_name });
  const body = await post(ENDPOINTS.KNOWLEDGE_DELETE_CONFIRM, {
    stored_name,
    confirm_token: preview.confirm_token,
  });
  return {
    deleted_chunks: body.deleted_chunks || 0,
    file_removed: !!body.file_removed,
  };
}
