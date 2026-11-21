import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  server: {
    // 浏览器始终访问同源 /api，避免前端开发端口与后端端口产生跨域问题。
    proxy: { '/api': 'http://127.0.0.1:8000' },
  },
});
