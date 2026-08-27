<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface StatusData {
  uptime_seconds: number
  database: boolean
  game_http_80: boolean
  game_tcp_15110: boolean
  game_tcp_15115: boolean
}

const loading = ref(true)
const data = ref<StatusData | null>(null)

async function load() {
  loading.value = true
  try {
    data.value = await get<StatusData>('/status', 8000)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '探测失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)

function fmtUptime(sec: number): string {
  const d = Math.floor(sec / 86400)
  const h = Math.floor((sec % 86400) / 3600)
  const m = Math.floor((sec % 3600) / 60)
  return `${d} 天 ${h} 小时 ${m} 分`
}

const checks: { key: keyof StatusData; label: string }[] = [
  { key: 'database', label: 'PostgreSQL 数据库' },
  { key: 'game_http_80', label: '游戏服 HTTP :80' },
  { key: 'game_tcp_15110', label: '游戏服 TCP :15110' },
  { key: 'game_tcp_15115', label: '统计服 TCP :15115' },
]

/** el-table 插槽 row 无类型，索引检查收敛到这里 */
function isOn(key: string): boolean {
  return Boolean(data.value && data.value[key as keyof StatusData])
}
</script>

<template>
  <div v-loading="loading">
    <div class="panel" style="margin-bottom: 20px">
      <div class="toolbar">
        <div style="font-weight: 700">服务健康检查</div>
        <el-button size="small" @click="load">重新探测</el-button>
      </div>
      <el-table v-if="data" :data="checks" size="small">
        <el-table-column prop="label" label="检查项" min-width="200" />
        <el-table-column label="状态" width="120">
          <template #default="{ row }">
            <el-tag :type="isOn(row.key) ? 'success' : 'danger'" size="small">
              {{ isOn(row.key) ? '在线' : '离线' }}
            </el-tag>
          </template>
        </el-table-column>
      </el-table>
    </div>

    <div class="stat-cards">
      <div class="stat">
        <div class="label">管理端运行时长</div>
        <div class="value" style="font-size: 16px">
          {{ data ? fmtUptime(data.uptime_seconds) : '-' }}
        </div>
      </div>
    </div>
  </div>
</template>
