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
  ban_reason: string
  ban_expires: string | null
  deleted_at: string | null
  created_at: string
  last_login_ip: string | null
  last_login_at: string | null
}

interface LoginEventRow {
  id: number
  username: string
  ip: string | null
  machine_id: string
  machine_id_old: string
  result: number
  source: string
  created_at: string
}

interface LoginProfile {
  login_events: LoginEventRow[]
  linked_accounts: { same_machine: string[]; same_ip: string[] }
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

// ---- 封禁/解封（列表行级：快速永久封禁；限时/原因/连带机器码进详情页）----
async function onToggleBan(row: PlayerRow) {
  const banned = !row.banned
  try {
    await ElMessageBox.confirm(
      banned
        ? `确认永久封禁玩家 ${row.username}？（限时封禁/原因/连带机器码请进玩家详情页操作）`
        : `确认解封玩家 ${row.username}？`,
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

/** 封禁状态标签（区分永久 / 限时未到期 / 限时已到期） */
function banTag(row: PlayerRow): { text: string; type: 'danger' | 'warning' } {
  if (row.ban_expires) {
    const expired = new Date(row.ban_expires).getTime() <= Date.now()
    return expired
      ? { text: '封禁已到期', type: 'warning' }
      : { text: `限时至 ${fmtTime(row.ban_expires)}`, type: 'danger' }
  }
  return { text: '永久封禁', type: 'danger' }
}

// ---- 展开行：登录记录懒加载（/players/{id}/logins，按行缓存）----
const loginCache = ref<Record<number, LoginProfile>>({})
const loginLoading = ref<Record<number, boolean>>({})

async function onExpand(row: PlayerRow, expanded: PlayerRow[]) {
  if (!expanded.some(r => r.id === row.id)) return   // 收起不加载
  if (loginCache.value[row.id] || loginLoading.value[row.id]) return
  loginLoading.value[row.id] = true
  try {
    loginCache.value[row.id] =
      await get<LoginProfile>(`/players/${row.id}/logins`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '登录记录加载失败')
  } finally {
    loginLoading.value[row.id] = false
  }
}

const loginResultText: Record<number, string> = { 97: '成功', 98: '密码错误' }
const loginSourceText: Record<string, string> = {
  game: '游戏登录', register: '注册', import: '迁移',
}

/** IP/机器码跳全局"登录记录"反查页 */
function searchLogins(q: string) {
  router.push({ name: 'login-events', query: { q } })
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
              @selection-change="(rows: PlayerRow[]) => selected = rows"
              @expand-change="onExpand">
      <el-table-column type="expand">
        <template #default="{ row }">
          <div v-loading="loginLoading[row.id]" class="expand-login">
            <template v-if="loginCache[row.id]">
              <div
                v-if="loginCache[row.id].linked_accounts.same_machine.length ||
                      loginCache[row.id].linked_accounts.same_ip.length"
                class="linked-accounts">
                <template
                  v-if="loginCache[row.id].linked_accounts.same_machine.length">
                  <strong>同机器码：</strong>
                  <el-tag v-for="u in loginCache[row.id].linked_accounts.same_machine"
                          :key="'m' + u" type="danger" size="small"
                          style="margin-right: 6px">{{ u }}</el-tag>
                </template>
                <template v-if="loginCache[row.id].linked_accounts.same_ip.length">
                  <strong>同 IP：</strong>
                  <el-tag v-for="u in loginCache[row.id].linked_accounts.same_ip"
                          :key="'i' + u" type="warning" size="small"
                          style="margin-right: 6px">{{ u }}</el-tag>
                </template>
              </div>
              <el-table v-if="loginCache[row.id].login_events.length"
                        :data="loginCache[row.id].login_events" size="small">
                <el-table-column label="时间" min-width="165">
                  <template #default="{ row: ev }">{{ fmtTime(ev.created_at) }}</template>
                </el-table-column>
                <el-table-column label="结果" width="90">
                  <template #default="{ row: ev }">
                    <el-tag :type="ev.result === 97 ? 'success' : 'danger'" size="small">
                      {{ loginResultText[ev.result] ?? ev.result }}
                    </el-tag>
                  </template>
                </el-table-column>
                <el-table-column label="来源" width="90">
                  <template #default="{ row: ev }">
                    {{ loginSourceText[ev.source] ?? ev.source }}
                  </template>
                </el-table-column>
                <el-table-column label="IP" min-width="130">
                  <template #default="{ row: ev }">
                    <el-link v-if="ev.ip" type="primary" @click="searchLogins(ev.ip)">
                      {{ ev.ip }}
                    </el-link>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
                <el-table-column label="机器码" min-width="280">
                  <template #default="{ row: ev }">
                    <el-link v-if="ev.machine_id" type="primary"
                             @click="searchLogins(ev.machine_id)">
                      {{ ev.machine_id }}
                    </el-link>
                    <span v-else>-</span>
                  </template>
                </el-table-column>
              </el-table>
              <el-empty v-else description="暂无登录记录" :image-size="50" />
            </template>
          </div>
        </template>
      </el-table-column>
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
      <el-table-column label="状态" width="190">
        <template #default="{ row }">
          <el-tag v-if="row.deleted_at" type="info" size="small">已删除</el-tag>
          <el-tag v-else-if="row.banned" :type="banTag(row).type" size="small">
            {{ banTag(row).text }}
          </el-tag>
          <el-tag v-else type="success" size="small">正常</el-tag>
        </template>
      </el-table-column>
      <el-table-column label="最近登录" min-width="185">
        <template #default="{ row }">
          <div v-if="row.last_login_at" class="last-login">
            <span>{{ fmtTime(row.last_login_at) }}</span>
            <span class="ip">{{ row.last_login_ip || '-' }}</span>
          </div>
          <span v-else class="never">从未登录</span>
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

.expand-login {
  padding: 6px 12px 10px 56px;
  min-height: 40px;
}

.linked-accounts {
  margin-bottom: 10px;
  line-height: 2;

  strong {
    color: #c8a86b;
    margin-right: 4px;
  }
}

.last-login {
  display: flex;
  flex-direction: column;
  line-height: 1.5;

  .ip {
    font-family: monospace;
    font-size: 12px;
    color: #8fa3b8;
  }
}

.never {
  color: #6b6b6b;
}
</style>
