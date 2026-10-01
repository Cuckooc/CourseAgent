# Web Frontend 前端

基于 **React 18 + Vite 5 + Ant Design 5 + Zustand** 的单页应用，提供流式对话、知识库管理、会话历史、审核与管理后台界面。构建后由后端 `control/app.py` 单端口托管。

## 📁 目录结构

```
web/frontend/
├── index.html            # 入口 HTML
├── package.json          # 依赖与脚本（dev/build/preview/lint）
├── vite.config.js        # Vite 配置（开发代理等）
├── .eslintrc.cjs         # ESLint 配置
├── .prettierrc           # Prettier 配置
└── src/
    ├── main.jsx          # 应用入口（挂载 AntD ConfigProvider）
    ├── App.jsx           # 根组件
    ├── router/           # 路由
    │   ├── index.jsx     # 路由表：登录/权限双层守卫 + 懒加载 + 角色默认首页分流
    │   └── nav-config.jsx # 主导航单一来源（侧边栏菜单与路由共用 MAIN_NAV）
    ├── api/              # 接口封装层（按业务域拆分）
    │   ├── contracts.js  # ⭐ 接口契约中心（后端路径/SSE 帧类型/上传常量）
    │   ├── chatApi.js    # 对话
    │   ├── sessionApi.js # 会话
    │   ├── knowledgeApi.js # 知识库
    │   ├── fileApi.js    # 文件上传
    │   ├── reviewApi.js  # 审核
    │   ├── adminApi.js   # 管理员
    │   ├── profileApi.js # 画像
    │   └── usageApi.js   # 用量
    ├── services/         # 基础设施
    │   ├── http.js       # 统一 HTTP（JWT 注入/超时/401 登出/错误抛出）
    │   ├── sse.js        # SSE 流式解析（text/event-stream 帧状态机）
    │   └── permission.js # 前端权限判定
    ├── stores/           # Zustand 全局状态
    │   ├── authStore.js  # 认证（JWT/身份/角色，登录/登出）
    │   ├── chatStore.js  # 对话（消息列表/流式状态/发送/停止/恢复）
    │   ├── sessionStore.js # 会话列表
    │   └── usageStore.js # 用量
    ├── components/       # 组件
    │   ├── chat/         # ChatInput / ChatSidebar / MessageList
    │   ├── common/       # ErrorBoundary / RequireAuth / RequirePermission
    │   ├── knowledge/    # 知识库组件
    │   └── layout/       # AppLayout（整体框架布局）
    ├── pages/            # 页面（懒加载）
    │   ├── LoginPage.jsx     # 登录/注册
    │   ├── ChatPage.jsx      # 智能对话（SSE 流式渲染）
    │   ├── HistoryPage.jsx   # 会话历史
    │   ├── KnowledgePage.jsx # 知识库管理
    │   ├── ReviewPage.jsx    # 文档审核
    │   ├── ProfilePage.jsx   # 个人画像
    │   ├── UsagePage.jsx     # 用量观测
    │   └── AdminUsersPage.jsx # 用户管理（管理员）
    └── styles/global.css # 全局样式
```

## 🏗️ 架构要点

### 路由与权限
- React Router v6 声明式路由，页面懒加载
- **双层守卫**: `RequireAuth`（登录态）→ `RequirePermission`（角色权限）
- 角色默认首页分流（学生 → 对话页，管理员 → 管理页）

### 状态管理
Zustand 轻量 store，按业务域拆分（auth/chat/session/usage），组件按需订阅。

### SSE 流式方案
`sse.js` 只负责**解析已建立的 Response**（text/event-stream 帧状态机），不负责建连/token/重试——建连由 http 层的 fetch 完成，恢复语义由 `chatStore` 的 recover 流程处理。帧类型常量集中维护在 `api/contracts.js`。

### 统一 HTTP
`http.js`: 自动注入 JWT → 超时控制 → 401 全局登出 → 非 2xx 与 `body.status=fail` 统一抛错。

## 🔧 开发命令

```bash
npm install     # 安装依赖
npm run dev     # 开发服务器（Vite 代理到后端）
npm run build   # 构建产物（供后端 SPA 托管）
npm run lint    # ESLint 检查
```
