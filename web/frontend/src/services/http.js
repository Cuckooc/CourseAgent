/**
 * @文件 services/http.js
 * @作用 统一 HTTP 封装（全站 JSON 接口的唯一出口），职责：
 *   1. 请求拦截：注入 JWT——Authorization: Bearer {token}，token 来自 authStore
 *      （经 bindAuth 注入的 tokenGetter，authStore 持久化于 sessionStorage）；
 *   2. 超时控制：AbortController，默认 30s（SSE 流式通道不走此封装）；
 *   3. 响应拦截：401 → 调注入的 onUnauthorized（登出 + 由守卫跳 /login 并带 redirect 回跳）；
 *      429 / 4xx / 5xx → 抛 ApiError（message 优先取响应体 message，否则用 HTTP 级默认文案）；
 *   4. HTTP 200 → 以 body.status 判定 success/fail（统一契约：不依赖 code 字段）；
 *   5. 另导出 getToken/notifyUnauthorized 供 SSE（chatApi）与 XHR 上传（fileApi）复用鉴权行为。
 *   注意：循环依赖规避——本模块不 import store 单例，token 与 401 处理器通过
 *   setTokenGetter 式的 bindAuth() 注入（由 main.jsx 在启动时绑定 authStore）。
 * @主要成员 ApiError、bindAuth、resetHttpBindings、getToken、notifyUnauthorized、
 *   request、get、post、put、postForm、del（模块内部：DEFAULT_TIMEOUT_MS、HTTP_DEFAULT_MESSAGES）
 * @被谁使用 api/ 下全部请求模块（get/post/put/del/postForm）、api/chatApi.js 与 api/fileApi.js
 *   （getToken/notifyUnauthorized）、main.jsx（bindAuth 启动绑定）。
 */

/** 默认请求超时时间（毫秒）：30 秒；超时由 AbortController.abort('timeout') 触发。 */
const DEFAULT_TIMEOUT_MS = 30000;

/**
 * @class ApiError
 * @classdesc 统一业务/HTTP 错误类型，携带状态码与机器可读原因，供页面按 reason 区分提示策略。
 * @extends Error
 */
export class ApiError extends Error {
  /**
   * @constructor
   * @param {number} httpStatus HTTP 状态码（0=网络错误/超时/取消等无响应场景）。
   * @param {string} message 用户可读文案。
   * @param {string} [reason='http'] 机器可读错误类别：timeout/network/aborted/unauthorized/http/business/format。
   */
  constructor(httpStatus, message, reason = 'http') {
    super(message);
    this.name = 'ApiError';
    this.httpStatus = httpStatus;
    this.reason = reason;
  }
}

/** token 获取器（模块私有）：由入口 main.jsx 经 bindAuth 注入，实际读 authStore.token，避免与 store 循环依赖。 */
let _tokenGetter = () => null;

/** 401 处理器（模块私有）：由入口注入，实际执行 authStore.logout（登出 + 跳转登录页由路由守卫接管）。 */
let _onUnauthorized = null;

/**
 * @function bindAuth
 * @description 注入鉴权依赖（应用启动时调用一次）：token 获取器与 401 未授权处理器。
 *   仅在新值存在时覆盖，保证重复调用不会清空绑定。
 * @param {object} param0
 * @param {() => (string|null)} param0.tokenGetter 返回当前 JWT 的函数（绑定 authStore.getState().token）。
 * @param {() => void} param0.onUnauthorized 401 回调（绑定 authStore.getState().logout）。
 * @returns {void}
 */
export function bindAuth({ tokenGetter, onUnauthorized }) {
  _tokenGetter = tokenGetter || _tokenGetter;
  _onUnauthorized = onUnauthorized || _onUnauthorized;
}

/**
 * @function resetHttpBindings
 * @description 测试用：重置 token 获取器与 401 处理器绑定到初始空实现。
 * @returns {void}
 */
export function resetHttpBindings() {
  _tokenGetter = () => null;
  _onUnauthorized = null;
}

/**
 * @function getToken
 * @description 读取当前已绑定的 JWT（供 SSE 独立通道 chatApi.streamChat 与 XHR 上传 fileApi.uploadFiles 复用）。
 * @returns {string|null} 当前 token（authStore.sessionStorage 中的 pbl_token），未登录为 null。
 */
export function getToken() {
  return _tokenGetter();
}

/**
 * @function notifyUnauthorized
 * @description 触发已绑定的 401 全局处理（供 SSE/XHR 通道在收到 401 时复用，与 request() 行为一致）。
 * @returns {void}
 */
export function notifyUnauthorized() {
  _onUnauthorized?.();
}

/** 非 2xx 响应（且响应体无 message）时的 HTTP 状态码默认用户文案；429 为限流/预算提示。 */
const HTTP_DEFAULT_MESSAGES = {
  400: '请求参数有误',
  403: '没有权限执行此操作',
  404: '请求的资源不存在',
  413: '文件超过大小限制',
  429: '操作过于频繁，请稍后再试',
  500: '服务器内部错误，请稍后重试',
  503: '服务暂不可用，请稍后重试',
};

/**
 * @function request
 * @description 发起 HTTP 请求的核心函数（POST/GET/PUT/DELETE 均收敛于此）：
 *   请求拦截注入 Authorization 头与 JSON Content-Type，用 AbortController 统一
 *   「30s 超时 + 外部 signal 取消」；响应拦截处理 401 全局登出、非 2xx 抛错、
 *   HTTP 200 按 body.status 业务判失败。
 * @param {string} method HTTP 方法（GET/POST/PUT/DELETE）。
 * @param {string} path 端点路径（以 / 开头的相对路径，取自 contracts.ENDPOINTS，可含 query）。
 * @param {object} [opts]
 * @param {object} [opts.json] JSON 请求体（自动序列化并设置 Content-Type）。
 * @param {FormData} [opts.form] 表单请求体（与 json 二选一，不手动设 Content-Type）。
 * @param {number} [opts.timeoutMs=30000] 超时毫秒数。
 * @param {AbortSignal} [opts.signal] 外部中止信号（如组件卸载/请求取消），联动内部 controller。
 * @returns {Promise<object>} 成功时返回完整响应体（status=success，业务字段平铺），交由 api 层再裁剪。
 * @throws {ApiError} 超时（reason='timeout'）/用户取消（'aborted'）/网络失败（'network'）/
 *   401（'unauthorized'，同时触发全局登出）/其他非 2xx（'http'）/body.status==='fail'（'business'）/
 *   200 空体或非 JSON（'format'）。
 */
export async function request(method, path, opts = {}) {
  const { json, form, timeoutMs = DEFAULT_TIMEOUT_MS, signal } = opts;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort('timeout'), timeoutMs);
  // 外部 signal（如组件卸载）联动超时 controller
  signal?.addEventListener('abort', () => controller.abort('client'), { once: true });

  const headers = {};
  const token = _tokenGetter();
  if (token) headers.Authorization = `Bearer ${token}`;
  if (json !== undefined) headers['Content-Type'] = 'application/json';

  let resp;
  try {
    resp = await fetch(path, {
      method,
      headers,
      body: json !== undefined ? JSON.stringify(json) : form,
      signal: controller.signal,
    });
  } catch (e) {
    clearTimeout(timer);
    if (controller.signal.reason === 'timeout') {
      throw new ApiError(0, '请求超时，请检查网络后重试', 'timeout');
    }
    if (controller.signal.reason === 'client') {
      throw new ApiError(0, '请求已取消', 'aborted');
    }
    throw new ApiError(0, '网络连接失败，请检查网络', 'network');
  }
  clearTimeout(timer);

  // 401：token 缺失/无效/过期 → 全局登出
  if (resp.status === 401) {
    _onUnauthorized?.();
    throw new ApiError(401, '登录已失效，请重新登录', 'unauthorized');
  }

  // 解析响应体（4xx/5xx 也可能有 JSON body，如统一错误格式）
  let body = null;
  try {
    body = await resp.json();
  } catch {
    body = null; // 空响应或非 JSON（如 503 HTML）
  }

  if (!resp.ok) {
    const message = (body && body.message) || HTTP_DEFAULT_MESSAGES[resp.status] || `请求失败（HTTP ${resp.status}）`;
    throw new ApiError(resp.status, message, 'http');
  }

  // HTTP 200：以契约 status 字段判定
  if (body && body.status === 'fail') {
    throw new ApiError(resp.status, body.message || '操作失败', 'business');
  }
  if (body === null) {
    // HTTP 200 但响应体为空或非 JSON：reason='format' 便于调用方区分
    // （页面初始化加载可降级为友好提示，用户主动操作仍展示原文案）
    throw new ApiError(resp.status, '响应格式异常', 'format');
  }
  return body;
}

/**
 * @function get
 * @description GET 方法快捷封装。
 * @param {string} path 端点路径（可含 query string）。
 * @param {object} [opts] 透传 request 选项（timeoutMs/signal 等）。
 * @returns {Promise<object>} 完整响应体。
 * @throws {ApiError}
 */
export const get = (path, opts) => request('GET', path, opts);

/**
 * @function post
 * @description POST 方法快捷封装（JSON 请求体）。
 * @param {string} path 端点路径。
 * @param {object} json 请求体对象（自动 JSON 序列化）。
 * @param {object} [opts] 透传 request 选项。
 * @returns {Promise<object>} 完整响应体。
 * @throws {ApiError}
 */
export const post = (path, json, opts) => request('POST', path, { json, ...opts });

/**
 * @function put
 * @description PUT 方法快捷封装（JSON 请求体）。
 * @param {string} path 端点路径。
 * @param {object} json 请求体对象（自动 JSON 序列化）。
 * @param {object} [opts] 透传 request 选项。
 * @returns {Promise<object>} 完整响应体。
 * @throws {ApiError}
 */
export const put = (path, json, opts) => request('PUT', path, { json, ...opts });

/**
 * @function postForm
 * @description POST 方法快捷封装（FormData 请求体，浏览器自动带 multipart boundary）。
 * @param {string} path 端点路径。
 * @param {FormData} form 表单数据。
 * @param {object} [opts] 透传 request 选项。
 * @returns {Promise<object>} 完整响应体。
 * @throws {ApiError}
 */
export const postForm = (path, form, opts) => request('POST', path, { form, ...opts });

/**
 * @function del
 * @description DELETE 方法快捷封装。
 * @param {string} path 端点路径。
 * @param {object} [opts] 透传 request 选项。
 * @returns {Promise<object>} 完整响应体。
 * @throws {ApiError}
 */
export const del = (path, opts) => request('DELETE', path, opts);
