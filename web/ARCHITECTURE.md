# PBL 智能课程咨询服务 — 前端架构设计文档

> 版本：v1.0 ｜ 状态：待评审 ｜ 日期：2026-09-11
>
> 依据：[docs/API.md](../docs/API.md)（后端 16 端点，基准 commit `3879c11`）
>
> 本文档经确认后，按[第 12 节实施计划](#12-实施计划与验收标准)分阶段落地。

## 1. 背景与现状分析

### 1.1 现状

前端为 Gradio 单文件实现（`web/index.py`，~700 行），采用 `requests` 直连 FastAPI 后端，已实现：注册、账号登录、邮箱验证码登录、会话列表/创建/切换、SSE 流式对话（含 status 状态帧渲染）、文件上传。

### 1.2 功能矩阵（前端 vs 后端能力）

| 后端能力 | 前端现状 |
|---|---|
| 注册 / 账号登录 / 邮箱登录 | ✅ 已接 |
| 会话列表 / 创建 / 切换 | ✅ 已接（记录由流式对话本地累积） |
| SSE 流式对话 + 状态帧 | ✅ 已接 |
| 文件上传入库 | ✅ 已接 |
| **会话详情 `/history/detail`** | ❌ 未接——切换会话后历史记录丢失 |
| **会话重命名 `/history/update_title`** | ❌ 未接 |
| **知识库管理 `/knowledge/*` 3 端点** | ❌ 未接 |
| **LLM 用量查看 `/admin/llm/usage`** | ❌ 未接 |

### 1.3 痛点（架构动因）

1. **单文件不可维护**：500+ 行内嵌 CSS、UI 构建与业务逻辑与 HTTP 调用三层混杂在一个文件。
2. **Gradio 能力天花板**：无前端路由（多视图靠 Tabs hack）、无真正的状态管理、无法实现会话重命名/知识库表格分页/删除确认等企业级交互；SSE 消费逻辑与 UI 更新强耦合在 generator 里。
3. **无持久化恢复**：刷新页面丢失登录态与会话上下文（token 仅存内存 `self.token`）。
4. **错误处理不成体系**：各 handler 各自 try/except，用户文案不统一。
5. **无法独立测试与构建**：无组件级单测、无 lint、无构建产物，部署依赖 Python 运行环境。

## 2. 设计目标与原则

**目标**：以企业级标准重建前端，完整覆盖后端 16 端点能力，支撑后续功能演进（多轮编辑、知识库运营、用量看板）。

| # | 原则 | 说明 |
|---|---|---|
| P1 | 分层解耦 | 视图 / 状态 / 服务（API 封装）/ 协议（SSE 解析）四层，单向依赖 |
| P2 | API 契约驱动 | 以 docs/API.md 为唯一契约，前端类型定义与之一一对应 |
| P3 | 渐进迁移 | ~~新旧前端共存~~（Gradio 已于 M2 前移除，React 前端为唯一入口） |
| P4 | 安全内建 | token 生命周期、XSS 防护、越权处理在架构层解决而非散落各处 |
| P5 | 可测可部署 | 组件可单测、构建产物为纯静态文件、支持 dev 代理与生产两种模式 |

## 3. 技术选型（决策记录）

| 方案 | 优势 | 劣势 | 结论 |
|---|---|---|---|
| Gradio 多文件重构 | 保持纯 Python 栈 | 路由/状态/复杂交互能力仍受限，SSE 体验受限，无法做表格/弹窗类运营界面 | ❌ 放弃 |
| Vue 3 + Vite | 模板语法门槛低、官方中文文档完善 | 团队最终决策倾向 React 生态（组件库与人才储备） | 备选 |
| **React 18 + Vite** | 生态最大、Ant Design 企业中后台组件最全、社区资料最丰富、跨团队协作友好 | JSX 心智负担略高 | ✅ **采用**（用户决策） |

**选型组合**（均为当前稳定版本）：

| 层 | 技术 | 用途 |
|---|---|---|
| 框架 | **React 18**（函数组件 + Hooks） | 视图层 |
| 构建 | Vite 5 | dev server / 打包 |
| 路由 | React Router 6 | 页面路由 + 登录守卫（`<RequireAuth>` 包裹） |
| 状态 | Zustand | auth / session / chat / usage 四个 store（轻量、样板少；Redux Toolkit 为规模化后备） |
| HTTP | fetch（封装）+ 原生 ReadableStream | JSON API 与 SSE（`EventSource` 不支持 POST，故统一 fetch） |
| UI 组件 | Ant Design 5 | 表格/表单/抽屉/确认弹窗/通知，覆盖知识库管理与会话操作 |
| 语言 | JavaScript（JSX + JSDoc 注释） | 首期不上 TS，降低迁移成本；目录与接口形状按 TS 可演进设计 |
| 规范 | ESLint + Prettier | 代码质量 |

React 生态组件选型说明：

- **Zustand 优于 Redux Toolkit（本项目场景）**：仅 4 个轻 store、无中间件需求，Zustand 免 provider、样板代码最少；若后续出现复杂派生状态再评估迁移。
- **Ant Design 优于 MUI**：企业中后台组件最全（Table/Drawer/Modal/Upload 开箱即用），中文文档完善，与本系统"运营页"定位匹配。
- SSE 流式渲染用 `useState`/`useRef` + 逐帧 `setState`，配合 `AbortController` 实现停止。

## 4. 总体架构

```
┌─────────────────────────────────────────────────────┐
│  视图层 views/ + components/                          │
│  LoginPage / ChatPage / KnowledgePage / UsagePage    │
├─────────────────────────────────────────────────────┤
│  路由层 router/                                      │
│  路由表 + 全局守卫（未登录 → /login）                  │
├─────────────────────────────────────────────────────┤
│  状态层 stores/（Zustand）                            │
│  authStore / sessionStore / chatStore / usageStore   │
├─────────────────────────────────────────────────────┤
│  服务层 services/                                    │
│  http.js（fetch 封装：JWT 注入/401 拦截/统一错误）      │
│  sse.js（SSE 帧解析状态机：status/delta/done/error）   │
│  authApi / sessionApi / chatApi / knowledgeApi ...   │
├─────────────────────────────────────────────────────┤
│  契约层 api/contracts.js                             │
│  与 docs/API.md 一一对应的端点常量与响应形状注释          │
└─────────────────────────────────────────────────────┘
                    │ HTTP (JSON / SSE)
          ┌─────────▼─────────┐
          │  FastAPI 后端 8000  │
          └───────────────────┘
```

依赖方向自上而下单向；视图层禁止直接发 HTTP，必须经 store 或 service。

## 5. 目录结构

```
web/frontend/
├── index.html
├── vite.config.js            # dev 代理 /chat /history /login /file /knowledge /admin → :8000
├── package.json
├── .eslintrc.cjs / .prettierrc
├── src/
│   ├── main.jsx              # 入口：createRoot + Router + AntD ConfigProvider(zhCN)
│   ├── App.jsx               # 布局 + <RequireAuth> 包裹的业务路由
│   ├── api/
│   │   ├── contracts.js      # 端点常量 + 响应形状注释（对应 docs/API.md）
│   │   ├── authApi.js        # register / loginAccount / sendEmailCode / loginEmail
│   │   ├── sessionApi.js     # list / detail / create / updateTitle / send
│   │   ├── chatApi.js        # sendMessage / streamChat(SSE)
│   │   ├── fileApi.js        # uploadFile(multipart)
│   │   ├── knowledgeApi.js   # list / detail / delete
│   │   └── usageApi.js       # llmUsage
│   ├── services/
│   │   ├── http.js           # 统一请求：token 注入、401 → 跳登录、status 判定
│   │   └── sse.js            # SSE 帧解析状态机（可单测的纯函数）
│   ├── stores/
│   │   ├── authStore.js      # token / user / login / logout（token 持久化 sessionStorage）
│   │   ├── sessionStore.js   # 会话列表 / 当前会话 / 创建 / 重命名
│   │   ├── chatStore.js      # 消息列表 / 流式状态机 / 发送与中断
│   │   └── usageStore.js     # 用量快照
│   ├── router/
│   │   └── index.jsx         # 路由表 + <RequireAuth> 守卫组件
│   ├── pages/
│   │   ├── LoginPage.jsx     # 账号 / 邮箱验证码 / 注册 三 Tab
│   │   ├── ChatPage.jsx      # 主聊天界面（对应现有聊天 Tab）
│   │   ├── KnowledgePage.jsx # 知识库管理（新增）
│   │   └── UsagePage.jsx     # LLM 用量看板（新增）
│   ├── components/
│   │   ├── chat/             # ChatSidebar / MessageList / MessageBubble / StatusTip / ChatInput
│   │   ├── knowledge/        # UploadDialog / ChunkTable / DeleteConfirm
│   │   ├── common/           # AppHeader / EmptyState / ErrorBanner / RequireAuth
│   │   └── layout/           # AppLayout（顶部导航 + 内容区）
│   └── styles/               # design tokens（变量）+ 全局样式
└── tests/                    # vitest + @testing-library/react：sse.js 状态机 / http.js 错误映射 / 关键组件
```

## 6. 路由设计

| 路径 | 页面 | 守卫 | 说明 |
|---|---|---|---|
| `/login` | LoginPage | 已登录 → 重定向 `/chat` | 未登录唯一可达页 |
| `/chat` | ChatPage | 需登录 | 默认页（`/` 重定向至此） |
| `/knowledge` | KnowledgePage | 需登录 | 上传列表 / 分块预览 / 删除 |
| `/usage` | UsagePage | 需登录 | LLM 用量看板 |
| `*` | — | — | 404 → 重定向 `/chat` |

守卫实现：`<RequireAuth>` 组件包裹全部业务路由（`useAuthStore` 读取 token，无 token 渲染 `<Navigate to="/login" replace />` 并携带 `redirect` 参数）；http.js 的 401 拦截（见 §8）兜底 token 过期场景。

## 7. 状态管理设计（Zustand）

每个 store 为独立模块（`create()`），组件按需订阅，免 Provider：

| Store | State | 关键 Action | 对应 API |
|---|---|---|---|
| auth | token, userName, email | loginAccount / loginEmail / register / logout | /login/* |
| session | sessions[], currentSessionId, titles | fetchList / create / rename / select | /history/list, /create, /update_title |
| chat | messages[]（含 streaming 态）, statusTip, abortController | send（SSE）, loadHistory, abort | /chat/stream, /history/detail |
| usage | usage{}, lastFetched | fetchUsage | /admin/llm/usage |

关键设计点：

- **token 存储于 `sessionStorage`**（Tab 关闭即失效，优于 localStorage 的 XSS 暴露面；企业内网场景可接受重新登录）。authStore 初始化时从 sessionStorage 恢复。
- **chat.messages 元素形态**：`{ role: 'user'|'assistant', content, streaming?: bool }`——流式中最后一条 assistant 消息 `streaming: true`，statusTip 单独成字段渲染为气泡内灰字。
- **会话切换 = loadHistory**：选中会话即调 `/history/detail` 拉取持久化记录（修复 §1.2 缺失能力），本地流式累积仅作当次会话的增量。

## 8. API 层设计

### 8.1 http.js 统一封装（规格）

```
request(method, path, { json?, form?, timeout? })
  1. 注入 Authorization: Bearer <token>（authStore）
  2. 发送 fetch，解析 JSON
  3. HTTP 401 → authStore.logout() + router.push('/login')（带 redirect 参数）
  4. HTTP 429 → 统一文案"操作过于频繁，请稍后再试"
  5. HTTP 4xx/5xx → 抛 ApiError{code, message}（message 取响应体 message 字段）
  6. HTTP 200 → 以 body.status 判定：success → 返回 body；fail → 抛 ApiError（body.message）
     （契约：不依赖 code 字段）
```

### 8.2 sse.js 帧解析状态机（纯函数，可单测）

```
parseSseStream(reader, onFrame)
  逐行读取 → 按 "data: " 前缀拆帧 → JSON.parse → onFrame(frame)
  解析失败的单帧丢弃不中断（与现 Gradio 行为一致）

chatStore.send 的帧处理：
  status → statusTip = frame.message（气泡内灰字）
  delta  → 清 statusTip；当前 assistant 消息 content += frame.content
  done   → streaming=false；sessionStore.refreshCurrent(frame.session_id, frame.title)
  error  → streaming=false；气泡尾部追加 ⚠️ message
```

`AbortController` 支持用户中途停止流式输出（fetch abort → 气泡保留已收内容）。

### 8.3 页面 × 端点映射矩阵

| 端点 | LoginPage | ChatPage | KnowledgePage | UsagePage |
|---|---|---|---|---|
| POST /login/register | ✅ | | | |
| POST /login/account | ✅ | | | |
| POST /login/email/code | ✅ | | | |
| POST /login/email | ✅ | | | |
| POST /history/list | | ✅ 侧栏 | | |
| POST /history/detail | | ✅ 切会话加载 | | |
| POST /history/create | | ✅ 新会话按钮 | | |
| POST /history/update_title | | ✅ 重命名（新增） | | |
| POST /history/send | | （调试用，可后置） | | |
| POST /chat/stream | | ✅ 核心链路 | | |
| POST /chat/send | | （备用，非流式回退） | | |
| POST /file/path | | | ✅ 上传入口 | |
| GET /knowledge/list | | | ✅ 列表 | |
| POST /knowledge/detail | | | ✅ 分块预览 | |
| POST /knowledge/delete | | | ✅ 删除确认 | |
| GET /admin/llm/usage | | | | ✅ 看板 |

## 9. 页面与组件设计要点

### ChatPage（核心界面，沿用现有交互语言）

- **左侧栏** `ChatSidebar`：用户信息 + 登出、新建会话、会话列表（右键/悬浮 → 重命名内联编辑、当前会话高亮）
- **主区** `MessageList`：气泡流（user 右 / assistant 左）；`StatusTip` 渲染 SSE status 帧（灰字 + loading 图标），首个 delta 到达即被内容替换
- **输入区** `ChatInput`：textarea（Enter 发送 / Shift+Enter 换行）+ 上传按钮 + 停止按钮（流式中可用）
- 空态：欢迎语 + 引导提问示例

### KnowledgePage（新增运营页）

- 顶部上传（AntD `Upload.Dragger`，限制 `.pdf/.txt/.md`，前端先行校验 + 失败错误映射）
- 表格列：文件名 / 分块数 / 字符数 / 状态（正常 | 孤儿）/ 操作（预览、删除）——AntD `Table`
- 预览 = `Drawer` 内分块表格（offset/limit 分页，对应 /knowledge/detail）
- 删除 = `Modal.confirm` 二次确认（对应 /knowledge/delete，展示 deleted_chunks 结果）

### UsagePage（新增看板）

- 卡片式按模型展示 requests / prompt_tokens / completion_tokens（AntD `Statistic`）
- 手动刷新按钮（限流 30 次/分钟，不做自动轮询）

## 10. 安全设计

| 项 | 方案 |
|---|---|
| Token 生命周期 | sessionStorage 持久化；401 全局拦截登出；logout 主动清理（前端无对应后端撤销接口，JWT 自然过期兜底） |
| XSS | React JSX 默认转义；回答内容使用 `{}` 插值，**禁止 `dangerouslySetInnerHTML` 渲染模型输出**（LLM 输出当前为纯文本） |
| 越权 | 前端不持有 user_id 逻辑，全部由后端 JWT 判定；越权响应（空 data / fail）如实展示 |
| 文件校验 | 前端做扩展名白名单**仅作体验优化**（提示更友好），真实校验以后端 magic bytes 为准，失败文案透传 |
| 敏感信息 | 无密钥入前端；生产 env 的 VITE_API_BASE 与后端 CORS 白名单对齐 |

## 11. 构建与部署

- **dev**：`vite` 启动 ：5173，`vite.config.js` 将 `/login /chat /history /file /knowledge /admin /healthz` 代理至 `http://127.0.0.1:8000`（避免 CORS，与后端 `cors_origins` 配置解耦；text/html 请求 bypass 回 index.html 防 SPA 路由冲突）
- **prod（已实现，单端口推荐）**：`vite build` 产出 `dist/`，FastAPI 条件托管（`frontend_dist` 配置，默认 `web/frontend/dist`，目录不存在则退化为纯 API）：
  - `/` 与前端路由（/login /chat 等 GET）→ `index.html`（catch-all SPA fallback，注册于所有显式路由**之后**，否则抢占 /healthz）
  - `/assets/*` → hash 静态资源；API/探针前缀不回退，保持 JSON 404
  - 探活以 `/healthz`（liveness）、`/readyz`（readiness）为准
  - nginx 托管 + 反代 API 留作多副本/生产化阶段的替代方案
- 构建产物纳入 `.gitignore`；CI 阶段可追加 `npm run build` 与 vitest

## 12. 实施计划与验收标准

| 阶段 | 内容 | 验收标准 |
|---|---|---|
| M1 骨架 | Vite+React18 工程、路由+RequireAuth 守卫、http.js/sse.js+单测、authStore、LoginPage | 登录成功进 /chat；未登录访问业务页重定向 /login；sse 单测过 |
| M2 聊天核心 | ChatPage 全量（侧栏/流式/状态帧/停止/上传）、会话列表+创建+**切换加载历史**+**重命名** | 与后端联调：status 帧首响应即渲染、delta 逐字、done 刷新列表、刷新页面后会话与记录可恢复 |
| M3 运营页 | KnowledgePage（上传/列表/预览/删除）+ UsagePage | 上传 pdf/txt/md 成功入库；孤儿可清理；删除后向量与文件均消失（对照 docs/API.md 行为） |
| M4 收尾 | 错误文案统一、空态/加载态、ESLint、生产构建+FastAPI 静态托管 | ✅ 已完成（2026-09-12）：`npm run build` 产物经 FastAPI `frontend_dist` 托管，8000 直访登录/聊天/知识库/用量/刷新恢复全链路验收通过；探针 /healthz /readyz 不受 catch-all 影响；ESLint 0 错误、vitest 25/25 |

**回滚策略**：Gradio 版已移除（`web/index.py` 删除、`gradio`/`requests` 依赖从 requirements.txt 移除）；前端缺陷以 git 回滚或修复推进；生产托管可通过 `frontend_dist=` 置空一键退化为纯 API 服务。

---

*评审通过后从 M1 开始实施；实施过程中如与 docs/API.md 契约出现偏差，以后端实际行为为准并回写 API 文档。*
