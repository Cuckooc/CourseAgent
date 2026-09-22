/**
 * @文件 api/contracts.js
 * @作用 前端唯一的后端接口契约中心：集中维护接口路径常量、统一响应契约说明、
 *       SSE 事件帧类型与阶段顺序枚举、文件上传前端校验常量，与 docs/API.md 一一对应。
 *       路径均为相对路径（无 host 前缀），由开发服务器代理或同源部署转发到 FastAPI。
 * @主要成员
 *   - ENDPOINTS：全部后端接口路径（按 login/history/chat/profile/file/knowledge/review/admin 分组）
 *   - SSE_FRAME：/chat/stream 的 SSE 帧类型枚举
 *   - SSE_STAGES：status 帧 stage 的产出顺序
 *   - UPLOAD_EXTENSIONS / UPLOAD_MAX_MB：上传白名单与大小上限
 * @被谁使用 api/ 下全部请求模块（ENDPOINTS）、services/sse.js（SSE_FRAME）、
 *   stores/authStore.js（登录相关 ENDPOINTS）、pages/KnowledgePage.jsx 与
 *   components/chat/ChatInput.jsx（UPLOAD_EXTENSIONS、UPLOAD_MAX_MB）。
 *
 * 统一响应契约：
 *   { status: 'success'|'fail', code, message, ...业务字段平铺 }
 *   - 判定以 status 为准（code 历史兼容，不统一）
 *   - 登录失败 HTTP 200 + fail（防枚举）；401 仅用于 JWT 缺失/无效
 */

/**
 * 后端接口路径常量表（键名为语义化别名，值为 HTTP 路径，前缀对应 control/*_control.py 的 APIRouter prefix）。
 * @constant {Object<string, string>}
 */
export const ENDPOINTS = {
  // 认证（公开）
  REGISTER: '/login/register',
  LOGIN_ACCOUNT: '/login/account',
  LOGIN_EMAIL_CODE: '/login/email/code',
  LOGIN_EMAIL: '/login/email',
  // 会话（JWT）
  HISTORY_LIST: '/history/list',
  HISTORY_DETAIL: '/history/detail',
  HISTORY_CREATE: '/history/create',
  HISTORY_UPDATE_TITLE: '/history/update_title',
  HISTORY_DELETE_PREVIEW: '/history/delete/preview',
  HISTORY_DELETE_CONFIRM: '/history/delete/confirm',
  HISTORY_UNDO_DELETE: '/history/undo_delete', // POST 无请求体；撤销最近一次软删除（一次性）
  HISTORY_SEND: '/history/send',
  // 对话（JWT）
  CHAT_SEND: '/chat/send',
  CHAT_STREAM: '/chat/stream', // SSE: status/delta/done/error 帧
  CHAT_RECOVER: '/chat/recover', // 网络中断恢复：completed(回补答案)/missing(请重发)
  CHAT_FEEDBACK: '/chat/feedback', // POST {session_id, message_index, rating:1|-1, comment?}
  // 用户画像（JWT）
  PROFILE: '/profile', // GET 读取 / PUT 修改（先暂存 7 天再落库）
  // 文件（JWT）
  FILE_UPLOAD: '/file/path', // multipart
  FILE_STATUS: '/file/status', // GET /file/status/{task_id} 异步上传进度查询
  // 知识库（JWT）
  KNOWLEDGE_LIST: '/knowledge/list', // GET
  KNOWLEDGE_DELETE_PREVIEW: '/knowledge/delete/preview',
  KNOWLEDGE_DELETE_CONFIRM: '/knowledge/delete/confirm',
  // 文档审核（JWT）
  REVIEW_LIST: '/review/list', // GET ?status=pending|approved|rejected&page=1&page_size=20
  REVIEW_ALL: '/review/all', // GET 审核员全量队列（仅 teacher/admin），支持 status/user_id 过滤
  REVIEW_DETAIL: '/review', // GET /review/{id}
  REVIEW_APPROVE: '/review', // POST /review/{id}/approve
  REVIEW_REJECT: '/review', // POST /review/{id}/reject
  // 管理（JWT）
  ADMIN_LLM_USAGE: '/admin/llm/usage', // GET
  ADMIN_LLM_USAGE_USERS: '/admin/llm/usage/users', // GET ?month=YYYY-MM
  ADMIN_USERS: '/admin/users', // GET 全部用户；PUT /admin/users/{id}/role 调整角色；POST /admin/users/{id}/deactivate/preview|confirm 两步注销
  // 登录态身份（JWT）
  LOGIN_ME: '/login/me', // GET 当前身份（实时回库），应用加载/聚焦时同步角色
};

/**
 * SSE 帧类型枚举（后端 POST /chat/stream，text/event-stream）。
 * @constant {Object<string, string>}
 */
export const SSE_FRAME = {
  STATUS: 'status', // { stage, message } 前置阶段提示，首帧 <1s
  DELTA: 'delta', // { content } 文本增量
  DONE: 'done', // { session_id, title, ai_output }
  ERROR: 'error', // { message }
};

/**
 * status 帧 stage 产出顺序常量：rewrite → intent → analysis → retrieval(可选) → summary。
 * @constant {string[]}
 */
export const SSE_STAGES = ['rewrite', 'intent', 'analysis', 'retrieval', 'summary'];

/**
 * 上传文件扩展名白名单（前端体验优化，选择文件时即拦截；真实校验以后端 magic bytes 为准）。
 * @constant {string[]}
 */
export const UPLOAD_EXTENSIONS = ['.pdf', '.txt', '.md'];

/**
 * 单文件大小上限（MB），与后端 upload_max_mb 默认值一致，超限前端直接拦截不发请求。
 * @constant {number}
 */
export const UPLOAD_MAX_MB = 50;
