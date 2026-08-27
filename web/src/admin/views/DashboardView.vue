<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface DashboardData {
  players: number
  today_registrations: number
  characters: number
  battles: number
  recent_players: { id: number; username: string; created_at: string }[]
  wardb_loaded: number
}

interface RegStat {
  days: number
  items: { day: string; count: number }[]
}

const loading = ref(true)
const data = ref<DashboardData | null>(null)
const regStat = ref<RegStat | null>(null)

onMounted(async () => {
  try {
    const [d, s] = await Promise.all([
      get<DashboardData>('/dashboard'),
      get<RegStat>('/stats/registrations?days=30'),
    ])
    data.value = d
    regStat.value = s
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
})

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
}

// 简易柱状图（纯 CSS，避免引图表库）
const maxCount = () =>
  Math.max(1, ...(regStat.value?.items.map((i) => i.count) ?? [1]))
</script>

<template>
  <div v-loading="loading">
    <div class="stat-cards">
      <div class="stat">
        <div class="label">玩家总数</div>
        <div class="value">{{ data?.players ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">今日注册</div>
        <div class="value">{{ data?.today_registrations ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">角色总数</div>
        <div class="value">{{ data?.characters ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">战斗场次</div>
        <div class="value">{{ data?.battles ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">WarDatabase 模板</div>
        <div class="value">{{ data?.wardb_loaded ?? '-' }}</div>
      </div>
    </div>

    <div class="panel" style="margin-bottom: 20px">
      <div style="font-weight: 700; margin-bottom: 12px">近 30 天注册趋势</div>
      <div v-if="regStat && regStat.items.length" class="chart">
        <div v-for="it in regStat.items" :key="it.day" class="chart-col"
             :title="`${it.day}: ${it.count}`">
          <div class="chart-bar" :style="{ height: `${(it.count / maxCount()) * 100}%` }" />
          <div class="chart-label">{{ it.day.slice(5) }}</div>
        </div>
      </div>
      <el-empty v-else description="暂无注册数据" :image-size="60" />
    </div>

    <div class="panel">
      <div style="font-weight: 700; margin-bottom: 12px">最近注册玩家</div>
      <el-table :data="data?.recent_players ?? []" size="small">
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="username" label="用户名" min-width="160" />
        <el-table-column label="注册时间" min-width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 140px;
  padding-top: 8px;

  .chart-col {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    height: 100%;

    .chart-bar {
      background: linear-gradient(180deg, #8b9a46, #55632b);
      border-radius: 2px 2px 0 0;
      min-height: 2px;
    }

    .chart-label {
      font-size: 9px;
      color: #7a7561;
      text-align: center;
      margin-top: 4px;
      white-space: nowrap;
      overflow: hidden;
    }
  }
}
</style>
