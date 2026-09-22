/**
 * @文件 ErrorBoundary.jsx
 * @作用 顶层错误边界：任何未捕获渲染错误兜底为企业级降级页面，
 * 提供"重试"（重载）而非白屏。仅捕获渲染期错误，异步错误由各业务处理。
 * @主要成员 ErrorBoundary（默认导出，React class 组件）
 * @被谁使用 src/main.jsx 引入，在 createRoot 渲染树最外层包裹整个应用
 * （<ErrorBoundary> 内含 ConfigProvider / AntdApp / BrowserRouter / App）
 */
import React from 'react';
import { Button, Result } from 'antd';

/**
 * 组件：ErrorBoundary
 * 作用：React 错误边界（class 组件）——捕获子树渲染期/生命周期/构造函数中的错误，
 * 用 antd Result 降级页替换崩溃 UI，避免整页白屏
 * 实例化/挂载位置：src/main.jsx 应用渲染树最外层
 * 数据来源：props.children（被包裹的整个应用，由 main.jsx 传入）；内部 state.hasError
 * 数据去向：不请求任何接口；componentDidCatch 仅 console.error（预留监控上报）；
 * 用户点「刷新页面」调 window.location.reload() 重载
 * @param {object} props 组件 props
 * @param {import('react').ReactNode} props.children 子渲染树（必填，main.jsx 传入整个应用）
 */
class ErrorBoundary extends React.Component {
  constructor(props) {
    super(props);
    // hasError：是否已捕获到渲染错误；true 时 render 输出降级页替代 children
    this.state = { hasError: false };
  }

  /**
   * @function getDerivedStateFromError（React 静态生命周期）
   * @description 渲染抛错后、render 前被调用：返回新 state 触发降级页渲染
   * 被谁触发：React 在子树渲染期抛出错误时自动调用（非手动）
   * @returns {{hasError:boolean}} 固定返回 { hasError: true }
   * @副作用 仅更新 state（纯函数语义，禁止内含副作用）
   */
  static getDerivedStateFromError() {
    return { hasError: true };
  }

  /**
   * @function componentDidCatch（React 生命周期）
   * @description 错误提交阶段回调：记录错误与组件栈（生产环境可在此对接监控上报）
   * 被谁触发：React 捕获子树错误并提交 DOM 后自动调用
   * @param {Error} error 抛出的错误对象
   * @param {{componentStack?:string}} info React 组件栈信息
   * @returns {void} 副作用为 console.error 打印（预留 /metrics 前端延伸上报点）
   */
  componentDidCatch(error, info) {
    // 生产环境可上报监控（对接 /metrics 体系的前端延伸）
    console.error('[ErrorBoundary]', error, info?.componentStack);
  }

  /**
   * @function render
   * @description 按 hasError 选择渲染降级页或正常子树
   * 被谁触发：React 渲染流程（state 变化后自动重新渲染）
   * @returns {import('react').ReactNode} 出错返回 antd Result 降级页（含 reload 按钮），否则返回 children
   */
  render() {
    if (this.state.hasError) {
      return (
        <Result
          status="error"
          title="页面出现异常"
          subTitle="请重试；若持续出现请联系管理员"
          extra={<Button type="primary" onClick={() => window.location.reload()}>刷新页面</Button>}
        />
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
