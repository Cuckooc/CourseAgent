/**
 * @文件 services/sse.js
 * @作用 SSE（Server-Sent Events）帧解析状态机，纯函数无副作用、可单测。
 *   流式连接的后端来源：POST /chat/stream（control/chat_control.py 的 chat_router，
 *   Content-Type: text/event-stream；连接由 api/chatApi.js 的 streamChat 用原生 fetch 发起，
 *   本模块只负责读取已建立的 Response，不负责建连/token/重试）。
 *   事件帧结构：每帧 `data: {json}\n\n`，JSON 内 type ∈ status | delta | done | error
 *   （枚举见 api/contracts.js 的 SSE_FRAME）。
 *   解析策略（与后端行为对齐）：
 *   - 逐行读取，非 "data: " 前缀行（空行/注释）跳过；
 *   - 单帧 JSON 解析失败：丢弃该帧、不中断流（与 Gradio 版行为一致）；
 *   - 流读取错误：向调用方报告（气泡保留已收内容并追加提示）；
 *   - AbortController 由调用方（chatStore，接 UI 停止按钮）持有；本模块不做自动重连，
 *     中断后的核实/补偿去向为 chatStore.recoverAfterInterrupt → api/chatApi.recoverChat（/chat/recover）。
 * @主要成员 consumeSseStream、createFrameDispatcher
 * @被谁使用 api/chatApi.js（consumeSseStream：在 streamChat 内消费响应流）、
 *   stores/chatStore.js（createFrameDispatcher：把帧翻译为消息状态变更）。
 */

import { SSE_FRAME } from '../api/contracts.js';

/**
 * @function consumeSseStream
 * @description 从 fetch Response body 以流式 reader + TextDecoder 逐帧读取 SSE，
 *   每解析出一帧合法 JSON 即回调 onFrame。
 * @param {Response} response 已返回 2xx 的 fetch 响应（其 body 为 ReadableStream）。
 * @param {(frame: object) => void} onFrame 帧回调（chatApi 透传 chatStore 的帧分发器）。
 * @returns {Promise<void>} 流正常结束或被用户 abort 时 resolve（abort 不视为错误）。
 * @throws {Error} 读流失败且非用户中止时抛出（如连接中断），由 chatStore.send 捕获后走中断恢复流程。
 */
export async function consumeSseStream(response, onFrame) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE 事件以空行分隔；按行处理最简单（后端每帧自带 \n\n）
      let newlineIdx;
      while ((newlineIdx = buffer.indexOf('\n')) !== -1) {
        const line = buffer.slice(0, newlineIdx).replace(/\r$/, '');
        buffer = buffer.slice(newlineIdx + 1);
        if (!line.startsWith('data: ')) continue;
        try {
          onFrame(JSON.parse(line.slice(6)));
        } catch {
          // 单帧解析失败：丢弃，不中断
        }
      }
    }
  } catch (e) {
    if (e && (e.name === 'AbortError' || e.name === 'AbortErr')) {
      return; // 用户主动停止：正常返回，已收内容由调用方保留
    }
    throw e; // 连接中断等真实错误：向上报告
  }
}

/**
 * @function createFrameDispatcher
 * @description 创建 SSE 帧处理分发器：把原始帧按 type 翻译为四个语义回调，
 *   供 chatStore.send 在发送消息时构造消息列表状态变更（未知帧类型忽略，保证向前兼容）。
 *   分发去向：
 *   - status → onStatus：更新气泡内灰色阶段提示（statusTip）；
 *   - delta  → onDelta：向最后一条 assistant 消息追加文本增量（首帧清空 statusTip）；
 *   - done   → onDone：回补完整 ai_output、结束流式并交棒 sessionStore.refreshCurrent；
 *   - error  → onError：保留已收正文，仅在气泡渲染错误警示行。
 * @param {object} handlers 回调集合。
 * @param {(stage: string, message: string) => void} [handlers.onStatus] 阶段提示回调。
 * @param {(content: string) => void} [handlers.onDelta] 文本增量回调。
 * @param {(frame: {session_id:number, title:string, ai_output:string, rolled_over?:boolean}) => void} [handlers.onDone]
 *   完成帧回调（含真实会话 id/标题，可能携带长对话自动滚换标记）。
 * @param {(message: string) => void} [handlers.onError] 错误帧回调。
 * @returns {(frame: object) => void} 可直接传给 consumeSseStream 的 onFrame 回调。
 */
export function createFrameDispatcher(handlers) {
  return (frame) => {
    if (!frame || typeof frame !== 'object') return;
    switch (frame.type) {
      case SSE_FRAME.STATUS:
        handlers.onStatus?.(frame.stage || '', frame.message || '');
        break;
      case SSE_FRAME.DELTA:
        if (frame.content) handlers.onDelta?.(frame.content);
        break;
      case SSE_FRAME.DONE:
        handlers.onDone?.(frame);
        break;
      case SSE_FRAME.ERROR:
        handlers.onError?.(frame.message || '服务处理失败');
        break;
      default:
        break; // 未知帧类型：忽略（向前兼容）
    }
  };
}
