<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface BattleRow {
  id: number
  mission_session_id: string
  mission_id: number
  mission_preset_id: number
  winner_faction_id: number
  defender_faction_id: number
  mission_time_seconds: number | null
  battle_start_time: string | null
  created_at: string
}

const loading = ref(true)
const items = ref<BattleRow[]>([])
const total = ref(0)
const page = reactive({ page: 1, page_size: 20 })

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: BattleRow[] }>(
      `/battles?page=${page.page}&page_size=${page.page_size}`)
    total.value = r.total
    items.value = r.items
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
}

function fmtDuration(sec: number | null): string {
  if (sec == null) return '-'
  const m = Math.floor(sec / 60)
  return `${m} 分 ${sec % 60} 秒`
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column prop="id" label="战报 ID" width="90" />
      <el-table-column prop="mission_session_id" label="战斗会话" min-width="170" />
      <el-table-column prop="mission_id" label="任务 ID" width="90" />
      <el-table-column prop="mission_preset_id" label="预设" width="90" />
      <el-table-column prop="winner_faction_id" label="获胜阵营" width="100" />
      <el-table-column prop="defender_faction_id" label="防守阵营" width="100" />
      <el-table-column label="战斗时长" min-width="120">
        <template #default="{ row }">{{ fmtDuration(row.mission_time_seconds) }}</template>
      </el-table-column>
      <el-table-column label="战斗开始" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.battle_start_time) }}</template>
      </el-table-column>
      <el-table-column label="记录时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination v-model:current-page="page.page" v-model:page-size="page.page_size"
                     :total="total" :page-sizes="[20, 50, 100]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>
  </div>
</template>
