/**
 * @文件 api/fileApi.js
 * @作用 文件上传相关 API：multipart 文件上传（单文件/文件夹多文件、可选知识库范围）、
 *       异步上传任务进度查询。
 *       上传通道单独使用 XMLHttpRequest（而非 http.js 的 fetch 封装）：
 *       fetch 无法获取「请求体上传进度」，XHR 的 xhr.upload.onprogress 可提供真实字节进度；
 *       但仍复用 http.js 绑定的 token 注入（getToken）与 401 全局登出（notifyUnauthorized），
 *       保持鉴权行为一致。进度查询走 http.js GET 通道。
 *       后端前缀 /file（control/file_control.py 的 file_router），上传端点限流 5 次/分钟。
 *       scope 取值：private（默认，用户私有）/ temp（会话临时，需 session_id）/ public（仅 teacher/admin）。
 * @主要成员 uploadFiles、getUploadStatus（模块内部：parseResponseBody、HTTP_DEFAULT_MESSAGES、UPLOAD_TIMEOUT_MS）
 * @被谁使用 pages/KnowledgePage.jsx（知识库页上传按钮 + task_id 轮询）、
 *   components/chat/ChatInput.jsx（对话输入框附件上传 + 临时知识库 scope=temp）。
 */
import { getToken, notifyUnauthorized, get } from '../services/http.js';
import { ENDPOINTS } from './contracts.js';

/** 上传请求超时时间（毫秒）：1 小时，覆盖大文件/多文件文件夹传输 + 服务端解析与向量入库（多文件并行处理）。 */
const UPLOAD_TIMEOUT_MS = 3600000; // 1 小时：大文件/多文件文件夹 + 服务端解析与向量入库（多文件并行处理）

/** 上传通道各 HTTP 状态码的用户可读默认文案（响应体无 message 时兜底；429 为预算/限流提示）。 */
const HTTP_DEFAULT_MESSAGES = {
  400: '文件处理失败',
  401: '登录已失效，请重新登录',
  403: '没有权限上传到该知识库',
  404: '请求的资源不存在',
  413: '文件大小超过限制（单个文件最大 50MB）',
  429: '上传过于频繁，请稍后再试（每分钟最多 5 次）',
  500: '服务器处理文件失败，请稍后重试',
  503: '服务暂不可用，请稍后重试',
};

/**
 * @function parseResponseBody（模块内部）
 * @description 解析 XHR 响应体为 JSON（容错：空体/非标准 Content-Type 均不抛错）。
 * @param {XMLHttpRequest} xhr 已完成的 XHR 对象。
 * @returns {object|null} 解析出的响应体；空体或 JSON 失败（如 502 HTML）返回 null，交由状态码默认文案处理。
 */
function parseResponseBody(xhr) {
  const text = xhr.responseText;
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null; // 502 HTML 等非 JSON 响应：交由状态码默认文案处理
  }
}

/**
 * @function uploadFiles
 * @description 上传文件（单文件或文件夹多文件合并为一个 multipart 请求）到指定知识库范围。
 *   对应后端接口：POST /file/path（file_router，multipart/form-data），
 *   表单字段 files（可重复）、scope、可选 session_id；XHR 头注入 Authorization: Bearer token。
 * @param {File[]} files 文件对象数组（来自 KnowledgePage 选择器或 ChatInput 附件选择，含文件夹展开结果）。
 * @param {object} [opts]
 * @param {'private'|'temp'|'public'} [opts.scope='private'] 知识库范围
 *   （页面按钮决定：知识库页 private/public，对话附件 temp）。
 * @param {number} [opts.sessionId] 会话 ID；scope='temp' 时随表单提交（来自 sessionStore.currentSessionId）。
 * @param {AbortSignal} [opts.signal] 外部中止信号（取消上传时 xhr.abort()）。
 * @param {(p: {loaded:number, total:number, percent:number}) => void} [onProgress]
 *   真实字节上传进度回调（percent 0-100）；100% 仅代表传输完成，服务端解析入库尚未结束。
 * @returns {Promise<{ status: string, message: string, files: Array, task_id?: string }>}
 *   同步上传返回 { status, message, files }；异步大上传返回 { status:'processing', message, task_id }，
 *   由页面/ChatInput 继续轮询 getUploadStatus，最终刷新知识库列表或提示附件就绪。
 * @throws {Error} 网络异常/超时（1 小时）/非 2xx/HTTP 200 且 body.status=fail；
 *   401 调 notifyUnauthorized() 登出；429 提示「每分钟最多 5 次」；取消时抛 name='AbortError' 的错误。
 */
export function uploadFiles(files, opts = {}, onProgress) {
  const { scope = 'private', sessionId, signal } = opts;

  return new Promise((resolve, reject) => {
    const form = new FormData();
    files.forEach((f) => form.append('files', f));
    form.append('scope', scope);
    if (sessionId) form.append('session_id', String(sessionId));

    const xhr = new XMLHttpRequest();
    xhr.open('POST', ENDPOINTS.FILE_UPLOAD);
    xhr.timeout = UPLOAD_TIMEOUT_MS;
    const token = getToken();
    if (token) xhr.setRequestHeader('Authorization', `Bearer ${token}`);

    if (signal) {
      signal.addEventListener('abort', () => xhr.abort(), { once: true });
    }

    // 真实上传进度（仅请求体发送阶段）
    if (typeof onProgress === 'function') {
      xhr.upload.onprogress = (e) => {
        if (!e.lengthComputable) return;
        const percent = Math.min(100, Math.round((e.loaded / e.total) * 100));
        onProgress({ loaded: e.loaded, total: e.total, percent });
      };
    }

    xhr.onload = () => {
      const body = parseResponseBody(xhr);
      if (xhr.status === 401) {
        notifyUnauthorized();
        reject(new Error(body?.message || HTTP_DEFAULT_MESSAGES[401]));
        return;
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        // 全部文件失败时后端返回 400；2xx 下仍以 body.status 判定（partial 可正常返回）
        if (body && body.status === 'fail') {
          reject(new Error(body.message || '文件处理失败'));
          return;
        }
        // 同步上传：{ status, message, files }
        // 异步大上传：{ status:'processing', message, task_id }
        resolve({
          status: body?.status || 'success',
          message: body?.message || '',
          files: body?.files || [],
          task_id: body?.task_id || undefined,
        });
        return;
      }
      reject(new Error(body?.message || HTTP_DEFAULT_MESSAGES[xhr.status] || `上传失败（HTTP ${xhr.status}）`));
    };

    xhr.onerror = () => reject(new Error('网络异常，文件上传失败，请检查网络后重试'));
    xhr.ontimeout = () => reject(new Error('上传超时（超过 1 小时），请重试或拆分后上传'));
    xhr.onabort = () => {
      const err = new Error('上传已取消');
      err.name = 'AbortError';
      reject(err);
    };

    xhr.send(form);
  });
}

/**
 * @function getUploadStatus
 * @description 查询异步上传任务进度（大文件/多文件走后台处理时，页面凭 task_id 轮询）。
 *   对应后端接口：GET /file/status/{task_id}（file_router），走 http.js GET 通道（10s 超时）。
 * @param {string} taskId 任务 ID（uploadFiles 返回 status='processing' 时给出）。
 * @returns {Promise<{ status: 'processing'|'success'|'failed', progress: number, message: string }>}
 *   完整响应体；KnowledgePage/ChatInput 按 status 决定继续轮询、完成提示或失败提示。
 * @throws {ApiError} 401 跳登录；任务不存在/429 等由调用方 catch 停止轮询并提示。
 */
export async function getUploadStatus(taskId) {
  return get(`${ENDPOINTS.FILE_STATUS}/${taskId}`, { timeoutMs: 10000 });
}
