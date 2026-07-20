<script setup>
// 技能使用统计面板（验收修复 B，新增）：渲染 SkillStore 的使用追踪数据。
// 数据契约（api/routes/routes_skills.py）：
// - GET /api/skills/stats → {skills[]{filename,title,usage_count,success_rate,last_used}}
//   （按 usage_count 降序；index.json 不存在时为空数组）
import { onMounted, ref } from 'vue';
import { ElMessage } from 'element-plus';
import { Refresh } from '@element-plus/icons-vue';
import { request } from '../../api/client';
import zh from '../../i18n/zh';

const t = zh.debug.skillStats;

const skills = ref([]);
const loading = ref(false);

function ratePercent(rate) {
  return Math.round((Number(rate) || 0) * 100);
}

function rateStatus(rate) {
  const p = ratePercent(rate);
  if (p >= 80) return 'success';
  if (p >= 50) return 'warning';
  return 'exception';
}

function fmtLastUsed(ts) {
  return ts ? String(ts).replace('T', ' ').slice(0, 16) : t.neverUsed;
}

async function loadStats() {
  loading.value = true;
  try {
    const data = await request('/api/skills/stats');
    skills.value = Array.isArray(data?.skills) ? data.skills : [];
  } catch (err) {
    ElMessage.error(`${t.loadFailed}: ${err.message}`);
  } finally {
    loading.value = false;
  }
}

onMounted(loadStats);
</script>

<template>
  <div class="skill-stats-panel">
    <div class="toolbar">
      <span></span>
      <el-button size="small" :icon="Refresh" :loading="loading" @click="loadStats">
        {{ zh.debug.refresh }}
      </el-button>
    </div>

    <el-table :data="skills" v-loading="loading" size="small" stripe>
      <el-table-column :label="t.colSkill" min-width="220">
        <template #default="{ row }">
          <div class="skill-cell">
            <span class="skill-title">{{ row.title || row.filename }}</span>
            <span class="skill-filename">{{ row.filename }}</span>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="usage_count" :label="t.colUsage" width="100" align="right" sortable />
      <el-table-column :label="t.colSuccessRate" width="180">
        <template #default="{ row }">
          <el-progress
            :percentage="ratePercent(row.success_rate)"
            :status="rateStatus(row.success_rate)"
            :stroke-width="10"
          />
        </template>
      </el-table-column>
      <el-table-column :label="t.colLastUsed" width="150" align="right">
        <template #default="{ row }">{{ fmtLastUsed(row.last_used) }}</template>
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

.skill-cell {
  display: flex;
  flex-direction: column;
}

.skill-title {
  font-weight: 600;
}

.skill-filename {
  font-size: 11px;
  color: var(--el-text-color-secondary);
  font-family: 'Cascadia Code', Consolas, monospace;
}
</style>
