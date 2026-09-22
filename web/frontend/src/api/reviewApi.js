/**
 * @文件 api/reviewApi.js
 * @作用 文档审核 API：审核记录分页查询（我的/全量队列）、单条详情、审核通过（可覆盖提取文本）、驳回。
 *       全部请求经 services/http.js 统一通道（Bearer token 注入、401 全局登出、status=fail 抛错）。
 *       后端前缀 /review（control/review_control.py 的 review_router）。
 *       审核流程：OCR/多模态提取 → pending → 所有者本人或 teacher/admin 审核员查看/编辑 →
 *       approve（文本入库到文档上传者私有库）/ reject（仅状态流转）。
 * @主要成员 fetchReviewList（我的审核）、fetchAllReviewList（审核员全量队列）、
 * fetchReviewDetail、approveReview、rejectReview
 * @被谁使用 pages/ReviewPage.jsx（我的/全部队列两个视图的 Tab/分页/筛选、
 * 详情弹窗查看与编辑、通过/驳回按钮，两个视图共用详情与审核动作）。
 */
import { get, post } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function fetchReviewList
 * @description 按状态分页查询当前用户的文档审核记录。
 *   对应后端接口：GET /review/list?status=&page=&page_size=（review_router）。
 * @param {object} [params]
 * @param {'pending'|'approved'|'rejected'} [params.status] 审核状态过滤（来自列表 Tab；不传则全部）。
 * @param {number} [params.page=1] 页码（来自分页器当前页）。
 * @param {number} [params.page_size=20] 每页条数（来自分页器 pageSize）。
 * @returns {Promise<{ data: Array, total: number, page: number, page_size: number }>}
 *   分页记录与总数（缺省字段用入参/0 兜底）；交由 ReviewPage 渲染列表与分页器。
 * @throws {ApiError} 401 跳登录；429 等错误由页面 catch 提示。
 */
export function fetchReviewList({ status, page = 1, page_size = 20 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  params.set('page', String(page));
  params.set('page_size', String(page_size));
  return get(`${ENDPOINTS.REVIEW_LIST}?${params}`).then((body) => ({
    data: body.data || [],
    total: body.total || 0,
    page: body.page || page,
    page_size: body.page_size || page_size,
  }));
}

/**
 * @function fetchAllReviewList
 * @description 审核员视图：分页查询所有用户的审核记录（教师全量审核队列）。
 *   对应后端接口：GET /review/all?status=&user_id=&page=&page_size=（review_router），
 *   仅 teacher/admin 角色可用（普通用户调用后端返回 403）。
 * @param {object} [params]
 * @param {'pending'|'approved'|'rejected'} [params.status] 审核状态过滤（来自列表 Tab）。
 * @param {number} [params.userId] 按上传用户 ID 精确过滤（来自队列页筛选框；不传则全部用户）。
 * @param {number} [params.page=1] 页码。
 * @param {number} [params.page_size=20] 每页条数。
 * @returns {Promise<{ data: Array, total: number, page: number, page_size: number }>}
 *   记录含 user_id（上传者）字段，交由 ReviewPage 全量视图渲染「上传者」列与分页器。
 * @throws {ApiError} 401 跳登录；403 非审核员角色；429 等由页面 catch 提示。
 */
export function fetchAllReviewList({ status, userId, page = 1, page_size = 20 } = {}) {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  if (userId) params.set('user_id', String(userId));
  params.set('page', String(page));
  params.set('page_size', String(page_size));
  return get(`${ENDPOINTS.REVIEW_ALL}?${params}`).then((body) => ({
    data: body.data || [],
    total: body.total || 0,
    page: body.page || page,
    page_size: body.page_size || page_size,
  }));
}

/**
 * @function fetchReviewDetail
 * @description 获取单条审核记录详情（含提取原文，供抽屉展示与编辑）。
 *   对应后端接口：GET /review/{reviewId}（review_router）。
 * @param {number|string} reviewId 审核记录 ID（来自列表行点击）。
 * @returns {Promise<object>} 审核详情（body.data）；交由 ReviewPage 详情抽屉渲染。
 * @throws {ApiError} 401 跳登录；记录不存在/越权返回错误，由页面 catch 提示。
 */
export function fetchReviewDetail(reviewId) {
  return get(`${ENDPOINTS.REVIEW_DETAIL}/${reviewId}`).then((body) => body.data);
}

/**
 * @function approveReview
 * @description 审核通过：可选提交编辑后的文本覆盖 OCR/多模态提取原文，通过后文档正式入库。
 *   本人审核或 teacher/admin 代审均走此接口；后端固定把知识写入文档上传者的私有库
 *   （scope=private，归属记录 owner，与当前调用者角色无关）。
 *   对应后端接口：POST /review/{reviewId}/approve（review_router），
 *   请求体 { edited_text, notes }（空值传 null）。
 * @param {number|string} reviewId 审核记录 ID（来自详情抽屉当前记录）。
 * @param {object} [param1]
 * @param {string} [param1.edited_text] 用户在详情抽屉中编辑后的文本（未编辑则 null）。
 * @param {string} [param1.notes] 审核备注（可选）。
 * @returns {Promise<object>} 完整响应体；交由 ReviewPage 提示成功、关闭抽屉并刷新列表。
 * @throws {ApiError} 401/429/状态非 pending 等错误，页面 catch 后 message.error 提示。
 */
export function approveReview(reviewId, { edited_text, notes } = {}) {
  return post(`${ENDPOINTS.REVIEW_APPROVE}/${reviewId}/approve`, {
    edited_text: edited_text || null,
    notes: notes || null,
  });
}

/**
 * @function rejectReview
 * @description 驳回审核记录（文档不入库）。
 *   对应后端接口：POST /review/{reviewId}/reject（review_router），请求体 { notes }。
 * @param {number|string} reviewId 审核记录 ID（来自详情抽屉当前记录）。
 * @param {string} notes 驳回原因/备注（来自驳回弹窗输入，空值由后端按默认处理）。
 * @returns {Promise<object>} 完整响应体；交由 ReviewPage 提示成功、关闭抽屉并刷新列表。
 * @throws {ApiError} 401/429/状态非 pending 等错误，页面 catch 后 message.error 提示。
 */
export function rejectReview(reviewId, notes) {
  return post(`${ENDPOINTS.REVIEW_REJECT}/${reviewId}/reject`, { notes });
}
