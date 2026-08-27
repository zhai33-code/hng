<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface AuditRow {
  id: number
  admin_name: string
  action: string
  target: string
  detail: Record<string, unknown>
  ip: string
  created_at: string
}

const loading = ref(true)
const items = ref<AuditRow[]>([])
const total = ref(0)
const page = reactive({ page: 1, page_size: 50 })

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: AuditRow[] }>(
      `/audit-log?page=${page.page}&page_size=${page.page_size}`)
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

const actionLabels: Record<string, string> = {
  reset_password: '重置密码',
  adjust_currency: '调整货币',
  set_ban: '封禁/解封',
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="admin_name" label="管理员" min-width="130" />
      <el-table-column label="操作" width="120">
        <template #default="{ row }">{{ actionLabels[row.action] || row.action }}</template>
      </el-table-column>
      <el-table-column prop="target" label="目标玩家" min-width="140" />
      <el-table-column label="详情" min-width="220">
        <template #default="{ row }">
          <code class="detail-json">{{ JSON.stringify(row.detail) }}</code>
        </template>
      </el-table-column>
      <el-table-column prop="ip" label="来源 IP" width="130" />
      <el-table-column label="时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination v-model:current-page="page.page" v-model:page-size="page.page_size"
                     :total="total" :page-sizes="[50, 100, 200]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.detail-json {
  font-size: 12px;
  color: #9aa46a;
  word-break: break-all;
}
</style>
