<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { fmtTime } from '../format'

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
const query = reactive({ action: '', admin: '', q: '', days: 0 })

// 操作类型 -> [中文标签, tag 颜色]（与后端 audit.record 调用点一一对应）
const actionLabels: Record<string, [string, 'success' | 'warning' | 'danger' | 'info']> = {
  login_ok: ['登录成功', 'success'],
  login_fail: ['登录失败', 'danger'],
  reset_password: ['重置密码', 'warning'],
  adjust_currency: ['调整货币', 'warning'],
  set_ban: ['封禁/解封', 'danger'],
  soft_delete: ['软删除', 'danger'],
  hard_delete: ['硬删除', 'danger'],
  resolve_report: ['处理举报', 'success'],
  reopen_report: ['重开举报', 'warning'],
  send_mail: ['发送邮件', 'success'],
  generate_vouchers: ['生成兑换码', 'warning'],
}

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: AuditRow[] }>(
      `/audit-log?page=${page.page}&page_size=${page.page_size}` +
      `&action=${query.action}&admin=${encodeURIComponent(query.admin)}` +
      `&q=${encodeURIComponent(query.q)}&days=${query.days}`)
    total.value = r.total
    items.value = r.items
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.page = 1
  load()
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <el-select v-model="query.action" style="width: 150px" @change="onSearch">
        <el-option label="全部操作" value="" />
        <el-option v-for="(v, k) in actionLabels" :key="k" :label="v[0]" :value="k" />
      </el-select>
      <el-input v-model="query.admin" placeholder="管理员" clearable
                style="width: 150px" @keyup.enter="onSearch" @clear="onSearch" />
      <el-input v-model="query.q" placeholder="目标玩家" clearable
                style="width: 170px" @keyup.enter="onSearch" @clear="onSearch" />
      <el-select v-model="query.days" style="width: 130px" @change="onSearch">
        <el-option label="全部时间" :value="0" />
        <el-option label="今天" :value="1" />
        <el-option label="近 7 天" :value="7" />
        <el-option label="近 30 天" :value="30" />
      </el-select>
      <el-button type="primary" @click="onSearch">搜索</el-button>
    </div>

    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column prop="admin_name" label="管理员" min-width="130" />
      <el-table-column label="操作" width="110">
        <template #default="{ row }">
          <el-tag v-if="actionLabels[row.action]" size="small"
                  :type="actionLabels[row.action][1]">
            {{ actionLabels[row.action][0] }}
          </el-tag>
          <span v-else>{{ row.action }}</span>
        </template>
      </el-table-column>
      <el-table-column prop="target" label="目标" min-width="150">
        <template #default="{ row }">{{ row.target || '-' }}</template>
      </el-table-column>
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
