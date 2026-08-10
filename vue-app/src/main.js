import { createApp } from 'vue';
import { createPinia } from 'pinia';
import ElementPlus from 'element-plus';
import 'element-plus/dist/index.css';
import 'element-plus/theme-chalk/dark/css-vars.css';
// 熊猫主题覆盖必须在 element-plus 样式之后引入
import './theme/element-panda.css';
import App from './App.vue';
import router from './router';
import { initPluginRegistry } from './plugins/registry';

const app = createApp(App);
app.use(createPinia());
app.use(router);
app.use(ElementPlus);
app.mount('#app');

// 插件视图注册放在 mount 之后（依赖已激活的 pinia）；失败不阻塞主应用。
initPluginRegistry(router).catch((err) => {
  console.error('[plugins] 插件视图注册失败:', err);
});
