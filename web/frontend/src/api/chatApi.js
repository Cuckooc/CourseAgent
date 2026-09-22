/**
 * @文件 api/chatApi.js
 * @作用 对话域 API 封装：网络中断恢复查询、SSE 流式对话通道、回答点赞/点踩反馈。
 *       流式接口不走 http.js 的 request()（那里按 JSON 解析响应体，不适用于 event-stream），
 *       改为原生 fetch 直连，但复用 http.js 绑定的 token 注入（getToken）与 401 全局处理
 *       （notifyUnauthorized → authStore.logout）；帧解析交给 services/sse.js 的纯函数
 *       （consumeSseStream + createFrameDispatcher）。
 *       后端前缀 /chat（control/chat_control.py 的 chat_router），路由级限流 10 次/分钟，
 *       且 forbid_admin——admin 角色禁止对话。
 * @主要成员 recoverChat、streamChat、submitFeedback
 * @被谁使用 stores/chatStore.js（recoverChat/streamChat：发送、中断恢复状态机）、
 *   components/chat/MessageList.jsx（submitFeedback：消息气泡点赞点踩按钮）。
 */
import { getToken, notifyUnauthorized, post } from '../services/http.js';
import { consumeSseStream } from '../services/sse.js';
import { ENDPOINTS } from './contracts.js';

/**
 * @function recoverChat
 * @description 网络中断恢复：查询该会话在短期记忆（当前会话已生成内容）中的最后一轮是否完整。
 *   服务端只读短期记忆、不查长期记忆，不会重新生成/重复调用模型。
 *   对应后端接口：POST /chat/recover（chat_router），请求体 { session_id }，走 http.js JSON 通道。
 * @param {number} sessionId 会话 ID（来自 sessionStore.currentSessionId；新会话无 id 时传 0）。
 * @returns {Promise<{ recover_status: 'completed'|'missing', user_content?: string, ai_output?: string, message?: string }>}
 *   解析后的 body.data；completed 时由 chatStore 回补答案气泡，missing 时提示用户重新发送；
 *   响应无 data 时兜底 { recover_status: 'missing' }。
 * @throws {ApiError} 401 跳登录；其他错误由 chatStore.recoverAfterInterrupt 静默视为不可恢复。
 */
export async function recoverChat(sessionId) {
  const body = await post(ENDPOINTS.CHAT_RECOVER, { session_id: sessionId || 0 });
  return body.data || { recover_status: 'missing' };
}

/**
 * @function streamChat
 * @description 发起流式对话：POST /chat/stream（text/event-stream），逐帧回调 onFrame，
 *   直到流结束或被用户 abort。请求头手动注入 Authorization: Bearer {authStore.token}。
 *   事件帧结构：每帧 `data: {json}\n\n`，帧 type ∈ status/delta/done/error（见 contracts.SSE_FRAME）。
 * @param {object} params
 * @param {string} params.userInput 用户输入文本（来自 ChatInput 表单，经 chatStore.send 透传）。
 * @param {number} [params.sessionId] 会话 ID（sessionStore.currentSessionId）；
 *   为 0/null/undefined 时不传，后端自动创建新会话（done 帧返回真实 id）。
 * @param {AbortSignal} [params.signal] 中止信号（chatStore 持有 AbortController，接停止按钮）。
 * @param {(frame: object) => void} params.onFrame 帧回调（chatStore 用 createFrameDispatcher 生成）。
 * @returns {Promise<void>} 流结束（含用户主动 abort）时 resolve。
 * @throws {ApiError|Error} 建连失败（非 2xx/网络错误）或读流中断；
 *   401 调 notifyUnauthorized() 抛「登录已失效」；429 抛「操作过于频繁，请稍后再试」预算/限流提示；
 *   AbortError（用户停止）静默返回不抛错。
 */
export async function streamChat({ userInput, sessionId, signal, onFrame }) {
  const headers = { 'Content-Type': 'application/json' };
  const token = getToken();
  if (token) headers.Authorization = `Bearer ${token}`;

  const body = { user_input: userInput };
  if (sessionId) body.session_id = sessionId;

  let resp;
  try {
    resp = await fetch(ENDPOINTS.CHAT_STREAM, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
      signal,
    });
  } catch (e) {
    if (e && e.name === 'AbortError') return; // 用户停止：静默返回
    throw new Error('网络连接失败，请检查网络');
  }

  if (resp.status === 401) {
    notifyUnauthorized();
    throw new Error('登录已失效，请重新登录');
  }
  if (!resp.ok || !resp.body) {
    // SSE 端点异常时 HTTP 层失败（如 429 限流、500）
    let message = `请求失败（HTTP ${resp.status}）`;
    if (resp.status === 429) message = '操作过于频繁，请稍后再试';
    try {
      const errBody = await resp.json();
      if (errBody && errBody.message) message = errBody.message;
    } catch {
      // 非 JSON 错误体：保留默认文案
    }
    throw new Error(message);
  }

  await consumeSseStream(resp, onFrame);
}

/**
 * @function submitFeedback
 * @description 提交对某条 AI 回答的点赞/点踩反馈（可选文字评论）。
 *   对应后端接口：POST /chat/feedback（chat_router），走 http.js JSON 通道，
 *   请求体 { session_id, message_index, rating, comment }。
 * @param {object} params
 * @param {number} params.sessionId 当前会话 ID（来自 sessionStore.currentSessionId）。
 * @param {number} params.messageIndex 该 assistant 消息在会话中的「轮次」
 *   （从 0 编号，按 role=assistant 计数；由 MessageList 渲染时得出）。
 * @param {1|-1} params.rating 评价：1=点赞，-1=点踩（来自气泡点赞/点踩按钮点击）。
 * @param {string} [params.comment] 可选文字反馈，最长 500 字；缺省传 null。
 * @returns {Promise<{ status: 'success' }>} 后端固定返回成功；交由 MessageList 更新按钮态/轻提示。
 * @throws {ApiError} rating 非 1/-1 或 comment 超 500 字时后端返回 fail（http.js 抛业务错）；401 跳登录。
 */
export async function submitFeedback({ sessionId, messageIndex, rating, comment }) {
  const body = await post(ENDPOINTS.CHAT_FEEDBACK, {
    session_id: sessionId,
    message_index: messageIndex,
    rating,
    comment: comment ?? null,
  });
  return { status: body.status || 'success' };
}
