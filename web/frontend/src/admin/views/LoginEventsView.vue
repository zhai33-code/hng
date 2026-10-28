<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { fmtTime } from '../format'

interface LoginRow {
  id: number
  player_id: number | null
  username: string
  ip: string | null
  machine_id: string
  machine_id_old: string
  result: number
  source: string
  created_at: string
}

interface SearchResult {
  total: number
  rows: LoginRow[]
  machines: Record<string, string[]>
  ips: Record<string, string[]>
}

const route = useRoute()
const router = useRouter()
const loading = ref(false)
const q = ref('')
const data = ref<SearchResult | null>(null)
const page = reactive({ page: 1, page_size: 50 })

const loginResultText: Record<number, string> = { 97: '成功', 98: '密码错误' }
const loginSourceText: Record<string, string> = {
  game: '游戏登录', register: '注册', import: '迁移',
}

/** 机器码 → 关联账号（>1 个才值得展示） */
const linkedMachines = computed(() =>
  Object.entries(data.value?.machines ?? {})
    .filter(([, users]) => users.length > 1))

async function load() {
  loading.value = true
  try {
    data.value = await get<SearchResult>(
      `/logins?q=${encodeURIComponent(q.value)}` +
      `&page=${page.page}&page_size=${page.page_size}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

function onSearch() {
  page.page = 1
  router.replace({ query: q.value ? { q: q.value } : {} })
  load()
}

function searchUser(u: string) {
  q.value = u
  onSearch()
}

function gotoPlayer(row: LoginRow) {
  if (row.player_id) router.push(`/players/${row.player_id}`)
}

// 深链（玩家详情页点击 IP/机器码跳入）+ 路由 query 变化时重搜
watch(() => route.query.q, (v) => {
  const s = typeof v === 'string' ? v : ''
  if (s !== q.value) {
    q.value = s
    page.page = 1
    load()
  }
})

onMounted(() => {
  q.value = typeof route.query.q === 'string' ? route.query.q : ''
  load()
})
</script>

<template>
  <div class="panel">
    <div class="toolbar">
      <el-input v-model="q" placeholder="用户名 / IP / 机器码（空 = 最近记录）" clearable
                style="width: 320px" @keyup.enter="onSearch" @clear="onSearch" />
      <el-button type="primary" @click="onSearch">搜索</el-button>
    </div>

    <div v-if="linkedMachines.length" class="linked-machines">
      <div v-for="[mid, users] in linkedMachines" :key="mid" class="linked-item">
        <span class="mid">{{ mid }}</span>
        <el-tag v-for="u in users" :key="u" :type="'danger'" size="small"
                style="margin-right: 6px; cursor: pointer" @click="searchUser(u)">
          {{ u }}
        </el-tag>
      </div>
    </div>

    <el-table v-loading="loading" :data="data?.rows ?? []" size="small">
      <el-table-column prop="id" label="ID" width="80" />
      <el-table-column label="时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="账号" min-width="150">
        <template #default="{ row }">
          <el-link v-if="row.player_id" type="primary" @click="gotoPlayer(row)">
            {{ row.username }}
          </el-link>
          <span v-else>{{ row.username }}</span>
        </template>
      </el-table-column>
      <el-table-column label="结果" width="90">
        <template #default="{ row }">
          <el-tag :type="row.result === 97 ? 'success' : 'danger'" size="small">
            {{ loginResultText[row.result] ?? row.result }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="来源" width="90">
        <template #default="{ row }">{{ loginSourceText[row.source] ?? row.source }}</template>
      </el-table-column>
      <el-table-column label="IP" min-width="130">
        <template #default="{ row }">
          <el-link v-if="row.ip" type="primary" @click="searchUser(row.ip)">{{ row.ip }}</el-link>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="机器码" min-width="280">
        <template #default="{ row }">
          <el-link v-if="row.machine_id" type="primary" @click="searchUser(row.machine_id)">
            {{ row.machine_id }}
          </el-link>
          <span v-else>-</span>
        </template>
      </el-table-column>
    </el-table>
    <el-empty v-if="!loading && data && !data.rows.length" description="无匹配记录" :image-size="60" />

    <div class="pager">
      <el-pagination v-model:current-page="page.page" v-model:page-size="page.page_size"
                     :total="data?.total ?? 0" :page-sizes="[50, 100, 200]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>
  </div>
</template>

<style lang="scss" scoped>
.linked-machines {
  margin-bottom: 14px;
  padding: 10px 14px;
  border: 1px solid #5c4a3a;
  border-radius: 6px;
  background: rgba(140, 60, 40, 0.12);

  .linked-item {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 4px;
    line-height: 2;

    .mid {
      font-size: 12px;
      color: #c8a86b;
      margin-right: 10px;
      font-family: monospace;
    }
  }
}
</style>
