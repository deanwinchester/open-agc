import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      // esm-bundler 构建包含模板编译器：插件视图（/static/plugins/* 下的
      // 原生 ES module）用模板字符串定义组件，需在运行时编译。
      vue: 'vue/dist/vue.esm-bundler.js',
    },
  },
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: '/static/vue/',
  build: {
    outDir: '../static/vue',
    emptyOutDir: true,
  },
});
