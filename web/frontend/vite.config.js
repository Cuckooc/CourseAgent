import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// 开发代理：前端 :5173 → 后端 :8000，避免 CORS（与后端 cors_origins 配置解耦）
const API_TARGET = 'http://127.0.0.1:8000';

const proxyPaths = ['/login', '/chat', '/history', '/file', '/knowledge', '/review', '/profile', '/admin', '/healthz', '/readyz', '/metrics'];

// 代理条目：API 请求转发后端；浏览器地址栏直达/刷新（Accept: text/html）
// 必须回退到 SPA index.html，否则 /login /chat 等与 API 前缀同名的前端路由
// 在硬导航时会被代理到后端返回 404（fetch/XHR 默认 Accept: */*，不受影响）。
const proxyEntry = {
  target: API_TARGET,
  changeOrigin: true,
  bypass(req) {
    if (req.headers.accept && req.headers.accept.includes('text/html')) {
      return '/index.html';
    }
    return undefined;
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: Object.fromEntries(proxyPaths.map((p) => [p, proxyEntry])),
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    chunkSizeWarningLimit: 1000, // antd 核心本身约 950KB（gzip 后约 300KB），属正常体积
    rollupOptions: {
      output: {
        // 第三方依赖分包：变动少、体积大的独立成 chunk，利于浏览器长缓存
        manualChunks: {
          react: ['react', 'react-dom', 'react-router-dom', 'zustand'],
          antd: ['antd'],
          'antd-icons': ['@ant-design/icons'],
        },
      },
    },
  },
});
