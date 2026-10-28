<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { fmtTime } from '../format'

interface ReportRow {
  id: number
  reporter_id: number
  reporter_name: string
  reported_player_id: number
  reported_name: string
  reported_banned: boolean
  category: number
  category_name: string
  action_game_session: number
  chatlogs: string
  created_at: string
  resolved_at: string | null
  resolved_by: string
}

const loading = ref(true)
const items = ref<ReportRow[]>([])
const total = ref(0)
const page = reactive({ page: 1, page_size: 20 })
const query = reactive({ state: 'pending', category: 0 })

const categories: Record<number, string> = {
  1: '挂机', 2: '骚扰辱骂', 3: '作弊', 4: '恶意破坏',
}

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: ReportRow[] }>(
      `/reports?page=${page.page}&page_size=${page.page_size}` +
      `&state=${query.state}&category=${query.category}`)
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

async function onResolve(row: ReportRow, resolved: boolean) {
  const action = resolved ? '标记为已处理' : '重新打开'
  try {
    await ElMessageBox.confirm(`确认对举报 #${row.id} ${action}？`, '确认',
      { type: 'warning' })
  } catch {
    return
  }
  try {
    await post(`/reports/${row.id}/resolve`, { resolved })
    ElMessage.success('操作成功')
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <el-select v-model="query.state" style="width: 130px" @change="onSearch">
        <el-option label="待处理" value="pending" />
        <el-option label="已处理" value="resolved" />
        <el-option label="全部" value="all" />
      </el-select>
      <el-select v-model="query.category" style="width: 130px" @change="onSearch">
        <el-option label="全部类别" :value="0" />
        <el-option v-for="(name, k) in categories" :key="k" :label="name" :value="k" />
      </el-select>
      <el-button type="primary" @click="onSearch">刷新</el-button>
    </div>

    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div class="chatlog-block">
            <div class="chatlog-title">聊天记录（战局会话 {{ row.action_game_session }}）</div>
            <pre class="chatlog-text">{{ row.chatlogs || '（无聊天记录）' }}</pre>
          </div>
        </template>
      </el-table-column>
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column label="类别" width="100">
        <template #default="{ row }">
          <el-tag size="small" :type="row.category === 3 ? 'danger'
            : row.category === 2 ? 'warning' : 'info'">
            {{ row.category_name }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="被举报玩家" min-width="150">
        <template #default="{ row }">
          <template v-if="row.reported_name">
            <RouterLink :to="`/players/${row.reported_player_id}`" class="link">
              {{ row.reported_name }}
            </RouterLink>
            <el-tag v-if="row.reported_banned" size="small" type="danger"
                    style="margin-left: 6px">已封禁</el-tag>
          </template>
          <span v-else class="dim">已删除 (#{{ row.reported_player_id }})</span>
        </template>
      </el-table-column>
      <el-table-column label="举报人" min-width="130">
        <template #default="{ row }">
          <RouterLink v-if="row.reporter_name" :to="`/players/${row.reporter_id}`"
                      class="link">{{ row.reporter_name }}</RouterLink>
          <span v-else class="dim">已删除 (#{{ row.reporter_id }})</span>
        </template>
      </el-table-column>
      <el-table-column label="举报时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="150">
        <template #default="{ row }">
          <el-tag v-if="row.resolved_at" size="small" type="success">已处理</el-tag>
          <el-tag v-else size="small" type="warning">待处理</el-tag>
          <span v-if="row.resolved_at" class="dim" style="display: block; font-size: 12px">
            {{ row.resolved_by }}
          </span>
        </template>
      </el-table-column>
      <el-table-column label="操作" width="180" fixed="right">
        <template #default="{ row }">
          <el-button v-if="!row.resolved_at" size="small" type="success"
                     @click="onResolve(row, true)">标记已处理</el-button>
          <el-button v-else size="small" @click="onResolve(row, false)">重新打开</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination v-model:current-page="page.page" v-model:page-size="page.page_size"
                     :total="total" :page-sizes="[20, 50, 100]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.link {
  color: #c8b98a;
  text-decoration: none;

  &:hover {
    color: #e8d48b;
  }
}

.dim {
  color: #6b6a5a;
}

.chatlog-block {
  padding: 4px 12px;

  .chatlog-title {
    color: #9a947e;
    font-size: 12px;
    margin-bottom: 6px;
  }

  .chatlog-text {
    margin: 0;
    font-size: 12px;
    color: #b5af9a;
    white-space: pre-wrap;
    word-break: break-all;
    max-height: 260px;
    overflow-y: auto;
  }
}
</style>
