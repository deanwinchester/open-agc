<script setup>
// Agent 效果指标面板（验收修复 B，新增）：聚合任务/步骤统计。
// 数据契约（api/routes/routes_tasks.py，纯 SELECT 聚合）：
// - GET /api/agent/effectiveness → {
//     status_counts{status:count}, tasks_total, tasks_last_7d, tasks_last_30d,
//     avg_steps_per_task, tool_calls_total, tool_success_rate,
//     top_tools[]{tool_name,calls,success_rate}
//   }
import { computed, onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.debug.agentStats;

const data = ref(null);
const loading = ref(false);

const statusRows = computed(() => {
  const counts = data.value?.status_counts || {};
  const total = data.value?.tasks_total || 0;
  return Object.entries(counts)
    .map(([status, count]) => ({
      status,
      label: zh.tasks.status[status] || status,
      count,
      percent: total ? Math.round((count / total) * 100) : 0,
    }))
    .sort((a, b) => b.count - a.count);
});

const toolSuccessPercent = computed(() =>
  Math.round((Number(data.value?.tool_success_rate) || 0) * 100)
);

function ratePercent(rate) {
  return Math.round((Number(rate) || 0) * 100);
}

async function loadStats() {
  loading.value = true;
  try {
    data.value = await request('/api/agent/effectiveness');
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

onMounted(loadStats);
</script>

<template>
  <div class="agent-stats-panel" v-loading="loading">
    <div class="toolbar">
      <span></span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="loadStats">
        {{ zh.debug.refresh }}
      </el-button>
    </div>

    <template v-if="data">
      <div class="stat-cards">
        <div class="stat-card">
          <div class="stat-value">{{ data.tasks_total }}</div>
          <div class="stat-label">{{ t.tasksTotal }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ data.tasks_last_7d }}</div>
          <div class="stat-label">{{ t.tasks7d }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ data.tasks_last_30d }}</div>
          <div class="stat-label">{{ t.tasks30d }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ data.avg_steps_per_task }}</div>
          <div class="stat-label">{{ t.avgSteps }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ data.tool_calls_total }}</div>
          <div class="stat-label">{{ t.toolCalls }}</div>
        </div>
        <div class="stat-card">
          <div class="stat-value">{{ toolSuccessPercent }}%</div>
          <div class="stat-label">{{ t.toolSuccess }}</div>
        </div>
      </div>

      <div v-if="!data.tasks_total" class="empty-state">
        <div class="empty-icon">📊</div>
        <p>{{ t.empty }}</p>
      </div>

      <div v-else class="stats-columns">
        <div class="stats-block">
          <div class="block-title">{{ t.statusTitle }}</div>
          <div v-for="row in statusRows" :key="row.status" class="status-row">
            <span class="status-label">{{ row.label }}</span>
            <el-progress
              class="status-bar"
              :percentage="row.percent"
              :stroke-width="12"
              :format="() => `${row.count}`"
            />
          </div>
        </div>

        <div class="stats-block">
          <div class="block-title">{{ t.topToolsTitle }}</div>
          <el-table :data="data.top_tools" size="small" stripe>
            <el-table-column prop="tool_name" :label="zh.debug.toolStats.colName" min-width="150" show-overflow-tooltip>
              <template #default="{ row }">
                <span class="tool-name">{{ row.tool_name }}</span>
              </template>
            </el-table-column>
            <el-table-column prop="calls" :label="zh.debug.toolStats.colCalls" width="90" align="right" />
            <el-table-column :label="zh.debug.skillStats.colSuccessRate" width="150">
              <template #default="{ row }">
                <el-progress
                  :percentage="ratePercent(row.success_rate)"
                  :status="ratePercent(row.success_rate) >= 80 ? 'success' : ratePercent(row.success_rate) >= 50 ? 'warning' : 'exception'"
                  :stroke-width="10"
                />
              </template>
            </el-table-column>
          </el-table>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 12px;
}

.stat-cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin-bottom: 20px;
}

.stat-card {
  background: var(--el-fill-color-light);
  border: 1px solid var(--el-border-color-lighter);
  border-radius: var(--panda-radius-card);
  padding: 14px 12px;
  text-align: center;
  transition: box-shadow var(--panda-transition), transform var(--panda-transition);
}

.stat-card:hover {
  box-shadow: var(--panda-shadow-card);
  transform: translateY(-1px);
}

.stat-value {
  font-size: 22px;
  font-weight: 700;
}

.stat-label {
  margin-top: 4px;
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.stats-columns {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
}

@media (max-width: 900px) {
  .stats-columns {
    grid-template-columns: 1fr;
  }
}

.block-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
}

.status-row {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.status-label {
  width: 72px;
  flex-shrink: 0;
  font-size: 12px;
  text-align: right;
}

.status-bar {
  flex: 1;
}

.tool-name {
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
}

.empty-state {
  padding: 24px 0;
  text-align: center;
  color: var(--el-text-color-secondary);
}
</style>
