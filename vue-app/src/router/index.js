import { createRouter, createWebHistory } from 'vue-router';
import ChatView from '../views/ChatView.vue';
import DebugView from '../views/DebugView.vue';
import TasksView from '../views/TasksView.vue';
import TaskDetailView from '../views/TaskDetailView.vue';
import GoalsView from '../views/GoalsView.vue';
import DownloadsView from '../views/DownloadsView.vue';
import SettingsLayout from '../views/settings/SettingsLayout.vue';
import ModelsView from '../views/settings/ModelsView.vue';
import SkillsView from '../views/settings/SkillsView.vue';
import McpView from '../views/settings/McpView.vue';
import PluginsView from '../views/settings/PluginsView.vue';

const routes = [
  { path: '/', redirect: '/chat' },
  { path: '/chat/:sessionId(\\d+)?', name: 'chat', component: ChatView, meta: { title: '对话' } },
  { path: '/tasks', name: 'tasks', component: TasksView, meta: { title: '任务' } },
  { path: '/tasks/:id(\\d+)', name: 'task-detail', component: TaskDetailView, meta: { title: '任务详情' } },
  { path: '/goals', name: 'goals', component: GoalsView, meta: { title: '目标' } },
  { path: '/downloads', name: 'downloads', component: DownloadsView, meta: { title: '下载' } },
  {
    path: '/settings',
    component: SettingsLayout,
    meta: { title: '设置' },
    children: [
      { path: '', redirect: '/settings/models' },
      { path: 'models', name: 'settings-models', component: ModelsView, meta: { title: '设置' } },
      { path: 'skills', name: 'settings-skills', component: SkillsView, meta: { title: '设置' } },
      { path: 'mcp', name: 'settings-mcp', component: McpView, meta: { title: '设置' } },
      { path: 'plugins', name: 'settings-plugins', component: PluginsView, meta: { title: '设置' } },
    ],
  },
  { path: '/debug', name: 'debug', component: DebugView, meta: { title: '调试' } },
  // 兜底：/app/ 下的未知路径一律回到对话页
  { path: '/:pathMatch(.*)*', redirect: '/chat' },
];

export default createRouter({
  history: createWebHistory('/app/'),
  routes,
});
