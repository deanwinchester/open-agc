<script setup>
// 调试视图（批次 1b + 验收修复 B）：子页签结构。
// - 日志：服务端日志 tail（GET /api/logs?lines=N → {lines: [...], total}）
// - 模型调用日志 / 工具调用统计 / 技能使用 / Agent 效果：见 components/debug/ 各面板
import { computed, nextTick, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { request } from '../api/client';
import zh from '../i18n/zh';
import ModelLogsPanel from '../components/debug/ModelLogsPanel.vue';
import ToolStatsPanel from '../components/debug/ToolStatsPanel.vue';
import SkillStatsPanel from '../components/debug/SkillStatsPanel.vue';
import AgentEffectivenessPanel from '../components/debug/AgentEffectivenessPanel.vue';

const t = zh.debug;

const LINE_OPTIONS = [100, 200, 500, 1000];

const activeTab = ref('logs');
const lines = ref(200);
const logLines = ref([]);
const total = ref(0);
const loading = ref(false);
const preRef = ref(null);

const logText = computed(() => (logLines.value.length ? logLines.value.join('\n') : t.empty));

async function loadLogs() {
  loading.value = true;
  try {
    const data = await request(`/api/logs?lines=${lines.value}`);
    logLines.value = Array.isArray(data?.lines) ? data.lines : [];
    total.value = data?.total ?? 0;
    // 日志为 tail 语义，最新在末尾，渲染后滚到底部
    await nextTick();
    if (preRef.value) preRef.value.scrollTop = preRef.value.scrollHeight;
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

function onLinesChange() {
  loadLogs();
}

onMounted(loadLogs);
</script>

<template>
  <div class="debug-view">
    <header class="view-header">
      <h1>{{ t.title }}</h1>
      <p class="view-desc">{{ t.desc }}</p>
    </header>

    <el-tabs v-model="activeTab" class="debug-tabs">
      <el-tab-pane :label="t.tabs.logs" name="logs" lazy>
        <el-card class="log-card" shadow="never">
          <div class="toolbar">
            <div class="toolbar-left">
              <span class="lines-label">{{ t.linesLabel }}</span>
              <el-select v-model="lines" class="lines-select" @change="onLinesChange">
                <el-option v-for="n in LINE_OPTIONS" :key="n" :label="n" :value="n" />
              </el-select>
              <el-button type="primary" :icon="Refresh" :loading="loading" @click="loadLogs">
                {{ t.refresh }}
              </el-button>
            </div>
            <span class="total-info">{{ t.totalPrefix }}{{ total }}{{ t.totalSuffix }}</span>
          </div>
          <pre ref="preRef" class="log-content" v-loading="loading">{{ logText }}</pre>
        </el-card>
      </el-tab-pane>

      <el-tab-pane :label="t.tabs.modelLogs" name="modelLogs" lazy>
        <el-card shadow="never">
          <ModelLogsPanel />
        </el-card>
      </el-tab-pane>

      <el-tab-pane :label="t.tabs.toolStats" name="toolStats" lazy>
        <el-card shadow="never">
          <ToolStatsPanel />
        </el-card>
      </el-tab-pane>

      <el-tab-pane :label="t.tabs.skillStats" name="skillStats" lazy>
        <el-card shadow="never">
          <SkillStatsPanel />
        </el-card>
      </el-tab-pane>

      <el-tab-pane :label="t.tabs.agentStats" name="agentStats" lazy>
        <el-card shadow="never">
          <AgentEffectivenessPanel />
        </el-card>
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped>
.debug-view {
  padding: 24px 28px 40px;
  max-width: 1080px;
  margin: 0 auto;
  display: flex;
  flex-direction: column;
  height: 100%;
  box-sizing: border-box;
}

.view-header h1 {
  margin: 0 0 6px;
  font-size: 20px;
}

.view-desc {
  margin: 0 0 20px;
  font-size: 13px;
  color: var(--el-text-color-secondary);
}

.debug-tabs {
  flex: 1;
  min-height: 0;
  display: flex;
  flex-direction: column;
}

/* tabs 内容区撑满剩余高度并可滚动：el-tabs 根默认 block 布局，50 行日志
   表格会溢出视口且无滚动条（用户反馈：调用日志无法上下滑动） */
.debug-tabs :deep(.el-tabs__content) {
  flex: 1;
  min-height: 0;
  overflow: auto;
}

.log-card {
  display: flex;
  flex-direction: column;
}

.log-card :deep(.el-card__body) {
  display: flex;
  flex-direction: column;
  flex: 1;
  min-height: 0;
}

.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
  flex-wrap: wrap;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.lines-label {
  font-size: 13px;
  color: var(--el-text-color-regular);
}

.lines-select {
  width: 100px;
}

.total-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.log-content {
  flex: 1;
  min-height: 300px;
  margin: 0;
  padding: 12px;
  overflow: auto;
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: 6px;
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-all;
}

@media (max-width: 768px) {
  .debug-view {
    padding: 16px 16px 32px;
  }

  .toolbar {
    flex-direction: column;
    align-items: stretch;
  }

  .toolbar-left {
    flex-wrap: wrap;
  }
}
</style>
