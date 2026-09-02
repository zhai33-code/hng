<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { fmtTime, fmtNum } from '../format'

interface PlayerRow {
  id: number
  username: string
  credits: number
  warfunds: number
  gold: number
  banned: boolean
  deleted_at: string | null
  created_at: string
}

const router = useRouter()
const loading = ref(true)
const deleting = ref(false)
const items = ref<PlayerRow[]>([])
const selected = ref<PlayerRow[]>([])
const total = ref(0)
const query = reactive({ q: '', page: 1, page_size: 20, order: 'created_desc', state: 'active' })

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: PlayerRow[] }>(
      `/players?page=${query.page}&page_size=${query.page_size}` +
      `&q=${encodeURIComponent(query.q)}&order=${query.order}&state=${query.state}`,
    )
    total.value = r.total
    items.value = r.items
    selected.value = []
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

function onSearch() {
  query.page = 1
  load()
}

async function onDelete(hard: boolean) {
  if (!selected.value.length) return
  const names = selected.value.map(p => p.username)
  const preview = names.length <= 5
    ? names.join('、')
    : `${names.slice(0, 5).join('、')} 等 ${names.length} 个账号`
  try {
    if (hard) {
      await ElMessageBox.confirm(
        `将永久删除 ${preview}，其角色、装备、货币等全部数据会被级联清除，且不可恢复！` +
        `（历史战报记录保留）`,
        '硬删除账号', { type: 'warning', confirmButtonText: '永久删除',
                       cancelButtonText: '取消' })
    } else {
      await ElMessageBox.confirm(
        `将软删除 ${preview}：账号数据保留，但该账号将无法登录。可通过置空 deleted_at 恢复。`,
        '软删除账号', { type: 'warning', confirmButtonText: '软删除',
                       cancelButtonText: '取消' })
    }
  } catch {
    return // 用户取消确认框
  }
  deleting.value = true
  try {
    const r = await post<{ count: number }>('/players/batch-delete', {
      ids: selected.value.map(p => p.id), hard,
    })
    ElMessage.success(`已${hard ? '硬' : '软'}删除 ${r.count} 个账号`)
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '删除失败')
  } finally {
    deleting.value = false
  }
}

// ---- 封禁/解封（列表行级）----
async function onToggleBan(row: PlayerRow) {
  const banned = !row.banned
  try {
    await ElMessageBox.confirm(
      `确认${banned ? '封禁' : '解封'}玩家 ${row.username}？`,
      banned ? '封禁玩家' : '解除封禁', { type: 'warning' })
  } catch {
    return
  }
  try {
    await post(`/players/${row.id}/ban`, { banned })
    ElMessage.success(banned ? '已封禁' : '已解封')
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
      <el-input v-model="query.q" placeholder="按用户名搜索" clearable
                style="width: 240px" @keyup.enter="onSearch" @clear="onSearch" />
      <el-select v-model="query.order" style="width: 170px" @change="onSearch">
        <el-option label="最新注册" value="created_desc" />
        <el-option label="最早注册" value="created_asc" />
        <el-option label="用户名 A→Z" value="name_asc" />
        <el-option label="用户名 Z→A" value="name_desc" />
      </el-select>
      <el-select v-model="query.state" style="width: 120px" @change="onSearch">
        <el-option label="正常账号" value="active" />
        <el-option label="已软删除" value="deleted" />
        <el-option label="全部" value="all" />
      </el-select>
      <el-button type="primary" @click="onSearch">搜索</el-button>
      <div class="spacer" />
      <el-button type="warning" :disabled="!selected.length" :loading="deleting"
                 @click="onDelete(false)">软删除{{ selected.length ? `(${selected.length})` : '' }}</el-button>
      <el-button type="danger" :disabled="!selected.length" :loading="deleting"
                 @click="onDelete(true)">硬删除{{ selected.length ? `(${selected.length})` : '' }}</el-button>
    </div>

    <el-table v-loading="loading" :data="items" size="small"
              @selection-change="(rows: PlayerRow[]) => selected = rows">
      <el-table-column type="selection" width="42"
                       :selectable="(row: PlayerRow) => !row.deleted_at" />
      <el-table-column prop="id" label="ID" width="80" sortable />
      <el-table-column label="用户名" min-width="170">
        <template #default="{ row }">
          <RouterLink :to="`/players/${row.id}`" class="player-link">
            {{ row.username }}
          </RouterLink>
        </template>
      </el-table-column>
      <el-table-column label="Credits" width="130" align="right">
        <template #default="{ row }">{{ fmtNum(row.credits) }}</template>
      </el-table-column>
      <el-table-column label="Warfunds" width="130" align="right">
        <template #default="{ row }">{{ fmtNum(row.warfunds) }}</template>
      </el-table-column>
      <el-table-column label="Gold" width="100" align="right">
        <template #default="{ row }">{{ fmtNum(row.gold) }}</template>
      </el-table-column>
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.deleted_at" type="info" size="small">已删除</el-tag>
          <el-tag v-else-if="row.banned" type="danger" size="small">已封禁</el-tag>
          <el-tag v-else type="success" size="small">正常</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="注册时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary"
                     @click="router.push(`/players/${row.id}`)">详情</el-button>
          <el-button v-if="!row.deleted_at" size="small" text
                     :type="row.banned ? 'success' : 'danger'"
                     @click="onToggleBan(row)">
            {{ row.banned ? '解封' : '封禁' }}
          </el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination v-model:current-page="query.page" v-model:page-size="query.page_size"
                     :total="total" :page-sizes="[20, 50, 100]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.spacer {
  flex: 1;
}

.player-link {
  color: #c9b96a;
  text-decoration: none;
  font-weight: 600;

  &:hover {
    text-decoration: underline;
  }
}
</style>
