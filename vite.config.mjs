import { defineConfig } from 'vite';

export default defineConfig({
  build: {
    lib: {
      entry: 'static/app.js',
      name: 'OpenAGC',
      formats: ['iife'],
      fileName: () => 'open-agc.min.js',
    },
    outDir: 'static/dist',
    emptyOutDir: false,
    minify: 'esbuild',
    sourcemap: false,
    cssMinify: true,
  },
});
