<script setup>
import { computed } from 'vue';
import { useRoute } from 'vue-router';
import zh from '../i18n/zh';

const route = useRoute();
const title = computed(() => route.meta.title || route.path);
</script>

<template>
  <div class="placeholder-view">
    <div class="empty-icon">🐼</div>
    <h2>{{ title }}</h2>
    <p>{{ zh.placeholder.migrating }}</p>
  </div>
</template>

<style scoped>
.placeholder-view {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 32px;
  color: var(--el-text-color-primary);
}

.placeholder-view .empty-icon {
  font-size: 40px;
  margin-bottom: 12px;
}

.placeholder-view h2 {
  margin: 0 0 8px;
  font-size: 20px;
  font-weight: 600;
}

.placeholder-view p {
  margin: 0;
  color: var(--el-text-color-secondary);
  font-size: 14px;
}
</style>
