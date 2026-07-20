<script setup>
// 工具调用统计面板（验收修复 B）：迁移旧 view-debug 的 tools 子页签。
// 数据契约（api/routes/routes_searxng.py）：
// - GET /api/tools/stats → {tools[]{name,type,calls,sessions,last_used,...}, summary{total_calls,total_tools}}
//   （数据源 data/tool_frequency.json，不存在时返回空数组）
import { onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.debug.toolStats;

const tools = ref([]);
const summary = ref({ total_calls: 0, total_tools: 0 });
const loading = ref(false);

function typeIcon(type) {
  return type === 'auto_tool' ? '⚙️' : type === 'mcp' ? '🔌' : '🔧';
}

async function loadStats() {
  loading.value = true;
  try {
    const data = await request('/api/tools/stats');
    tools.value = Array.isArray(data?.tools) ? data.tools : [];
    summary.value = data?.summary || { total_calls: 0, total_tools: 0 };
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

onMounted(loadStats);
</script>

<template>
  <div class="tool-stats-panel">
    <div class="toolbar">
      <span class="summary-info">
        {{ t.summaryPrefix }}{{ summary.total_tools }}{{ t.summaryMiddle }}{{ summary.total_calls }}{{ t.summarySuffix }}
      </span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="loadStats">
        {{ zh.debug.refresh }}
      </el-button>
    </div>

    <el-table :data="tools" v-loading="loading" size="small" stripe>
      <el-table-column :label="t.colName" min-width="200" show-overflow-tooltip>
        <template #default="{ row }">
          <span class="tool-name">{{ typeIcon(row.type) }} {{ row.name }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="calls" :label="t.colCalls" width="110" align="right" sortable />
      <el-table-column prop="sessions" :label="t.colSessions" width="100" align="right" sortable />
      <el-table-column :label="t.colType" width="110" align="center">
        <template #default="{ row }">
          <el-tag size="small" type="info" disable-transitions>{{ row.type || 'builtin' }}</el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="last_used" :label="t.colLastUsed" width="160" align="right">
        <template #default="{ row }">{{ row.last_used || '-' }}</template>
      </el-table-column>
      <template #empty>{{ t.empty }}</template>
    </el-table>
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

.summary-info {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.tool-name {
  font-family: 'Cascadia Code', Consolas, monospace;
  font-size: 12px;
}
</style>
