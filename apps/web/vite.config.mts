import {defineConfig} from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8000',
    },
  },
  test: {
    environment: 'happy-dom',
    include: ['tests/**/*.test.{ts,tsx}'],
    // 让 tests/stylesheet.test.ts 的 `?raw` 样式表导入拿到真实 CSS 文本
    // （默认 vitest 会把 CSS 模块（含 ?raw）stub 成空字符串）
    css: true,
  },
});
