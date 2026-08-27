<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface PlayerRow {
  id: number
  username: string
  credits: number
  warfunds: number
  gold: number
  banned: boolean
  created_at: string
}

const router = useRouter()
const loading = ref(true)
const items = ref<PlayerRow[]>([])
const total = ref(0)
const query = reactive({ q: '', page: 1, page_size: 20, order: 'created_desc' })

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: PlayerRow[] }>(
      `/players?page=${query.page}&page_size=${query.page_size}` +
      `&q=${encodeURIComponent(query.q)}&order=${query.order}`,
    )
    total.value = r.total
    items.value = r.items
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

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
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
      <el-button type="primary" @click="onSearch">搜索</el-button>
    </div>

    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column prop="id" label="ID" width="80" sortable />
      <el-table-column label="用户名" min-width="170">
        <template #default="{ row }">
          <RouterLink :to="`/players/${row.id}`" class="player-link">
            {{ row.username }}
          </RouterLink>
        </template>
      </el-table-column>
      <el-table-column prop="credits" label="Credits" width="110" />
      <el-table-column prop="warfunds" label="Warfunds" width="110" />
      <el-table-column prop="gold" label="Gold" width="90" />
      <el-table-column label="状态" width="90">
        <template #default="{ row }">
          <el-tag v-if="row.banned" type="danger" size="small">已封禁</el-tag>
          <el-tag v-else type="success" size="small">正常</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="注册时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="90" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary"
                     @click="router.push(`/players/${row.id}`)">详情</el-button>
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
.player-link {
  color: #c9b96a;
  text-decoration: none;
  font-weight: 600;

  &:hover {
    text-decoration: underline;
  }
}
</style>
