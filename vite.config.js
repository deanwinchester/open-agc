import { defineConfig } from 'vite';
import { resolve } from 'path';

export default defineConfig({
  build: {
    lib: {
      entry: resolve(__dirname, 'static/app.js'),
      name: 'OpenAGC',
      formats: ['iife'],
      fileName: () => 'open-agc.min.js',
    },
    outDir: resolve(__dirname, 'static/dist'),
    emptyOutDir: false,
    minify: 'terser',
    sourcemap: false,
  },
});
