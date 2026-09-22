/**
 * @文件 src/main.jsx
 * @作用 前端应用入口（由 index.html 以 module 方式加载），启动时完成两件事：
 *   1. 依赖注入：bindAuth 把 authStore 的 token 读取与 401 登出回调绑定到 services/http.js
 *      （采用注入而非直接 import，规避 http.js ↔ authStore 循环依赖）；
 *   2. 挂载 React 组件树：createRoot(#root) 渲染，由外到内的 Provider/全局组件结构为
 *      React.StrictMode（开发期双调用校验）
 *      → ErrorBoundary（顶层渲染崩溃兜底错误页）
 *      → ConfigProvider locale=zhCN（Ant Design 中文语境）
 *      → AntdApp（提供 message/modal/notification 静态实例上下文）
 *      → BrowserRouter（HTML5 history 路由，开启 v7 future flags 消除告警）
 *      → App（业务根：内含全量路由表 AppRoutes）。
 *   数据初始化去向：token/用户信息的首次恢复发生在 authStore 初始 state（sessionStorage），
 *   角色实时同步（GET /login/me）由业务视图在加载/聚焦时调 authStore.syncIdentity 触发；
 *   全局样式 styles/global.css 在此导入。
 * @主要成员 无导出（入口副作用模块）；启动期绑定 bindAuth、createRoot().render()
 * @被谁使用 index.html 的 <script type="module" src="/src/main.jsx">；挂载根节点 #root。
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import { App as AntdApp, ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import App from './App.jsx';
import ErrorBoundary from './components/common/ErrorBoundary.jsx';
import { bindAuth } from './services/http.js';
import { useAuthStore } from './stores/authStore.js';
import './styles/global.css';

// 启动期依赖注入：http.js 请求拦截器的 Bearer token 来源为 authStore（sessionStorage 恢复）
bindAuth({
  tokenGetter: () => useAuthStore.getState().token,
  // 401 全局拦截：清登录态（业务代码各自收到 ApiError 后由守卫跳登录）
  onUnauthorized: () => useAuthStore.getState().logout(),
});

createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary>
      <ConfigProvider locale={zhCN}>
        <AntdApp>
          {/* future flags: 提前对齐 react-router v7 行为，消除 Future Flag Warning */}
          <BrowserRouter future={{ v7_startTransition: true, v7_relativeSplatPath: true }}>
            <App />
          </BrowserRouter>
        </AntdApp>
      </ConfigProvider>
    </ErrorBoundary>
  </React.StrictMode>
);
