<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'

interface CharacterRow {
  id: number
  name: string
  faction: number
  faction_name: string
  template: number
  class_name: string
  combat_xp: number
  rank: number
  badge_0: string
  badge_1: string
  created_at: string
}

interface PlayerDetail {
  player: {
    id: number
    username: string
    credits: number
    warfunds: number
    gold: number
    banned: boolean
    created_at: string
  }
  characters: CharacterRow[]
  recent_battles: {
    id: number
    mission_id: number
    winner_faction_id: number
    battle_start_time: string | null
    created_at: string
  }[]
}

const route = useRoute()
const playerId = computed(() => Number(route.params.id))
const loading = ref(true)
const data = ref<PlayerDetail | null>(null)

async function load() {
  loading.value = true
  try {
    data.value = await get<PlayerDetail>(`/players/${playerId.value}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
}

function fmtNum(n: number): string {
  return (n ?? 0).toLocaleString('en-US')
}

/** 军衔徽章：客户端 images/Rank/{faction}_{branch}_{rank}.png（找不到则隐藏） */
function rankIcon(c: CharacterRow): string {
  return `/icons/Rank/${c.faction}_1_${c.rank}.png`
}
function onImgError(e: Event) {
  ;(e.target as HTMLElement).style.display = 'none'
}

// ---- 重置密码 ----
async function onResetPassword() {
  let pw: string
  try {
    const r = await ElMessageBox.prompt(
      '输入新密码（4-64 位，明文写入数据库，与游戏服存储方式一致）',
      '重置玩家密码', { inputType: 'password', inputPattern: /^.{4,64}$/,
        inputErrorMessage: '密码长度需为 4-64 位' })
    pw = r.value
  } catch {
    return
  }
  try {
    await post(`/players/${playerId.value}/reset-password`, { new_password: pw })
    ElMessage.success('密码已重置')
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

// ---- 货币调整 ----
const currencyForm = reactive({ currency: 'credits', delta: 1000 })

async function onAdjustCurrency() {
  const names: Record<string, string> = {
    credits: 'Credits', warfunds: 'Warfunds', gold: 'Gold',
  }
  const label = names[currencyForm.currency]
  const delta = Number(currencyForm.delta)
  try {
    await ElMessageBox.confirm(
      `确认对玩家 ${data.value?.player.username} ${delta >= 0 ? '发放' : '扣减'} ` +
      `${Math.abs(delta)} ${label}？`, '调整货币', { type: 'warning' })
  } catch {
    return
  }
  try {
    const r = await post<{ ok: boolean; balance: number }>(
      `/players/${playerId.value}/currency`,
      { currency: currencyForm.currency, delta },
    )
    ElMessage.success(`已调整，当前余额 ${r.balance}`)
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

// ---- 封禁 ----
async function onToggleBan() {
  const banned = !data.value?.player.banned
  try {
    await ElMessageBox.confirm(
      `确认${banned ? '封禁' : '解封'}玩家 ${data.value?.player.username}？`,
      banned ? '封禁玩家' : '解除封禁', { type: 'warning' })
  } catch {
    return
  }
  try {
    await post(`/players/${playerId.value}/ban`, { banned })
    ElMessage.success(banned ? '已封禁' : '已解封')
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}
</script>

<template>
  <div v-loading="loading">
    <template v-if="data">
      <div class="stat-cards">
        <div class="stat">
          <div class="label">用户名</div>
          <div class="value" style="font-size: 19px">{{ data.player.username }}</div>
        </div>
        <div class="stat">
          <div class="label">Credits</div>
          <div class="value num" :class="{ long: data.player.credits >= 10_000_000 }">{{ fmtNum(data.player.credits) }}</div>
        </div>
        <div class="stat">
          <div class="label">Warfunds</div>
          <div class="value num" :class="{ long: data.player.warfunds >= 10_000_000 }">{{ fmtNum(data.player.warfunds) }}</div>
        </div>
        <div class="stat">
          <div class="label">Gold</div>
          <div class="value num" :class="{ long: data.player.gold >= 10_000_000 }">{{ fmtNum(data.player.gold) }}</div>
        </div>
        <div class="stat">
          <div class="label">账号状态</div>
          <div class="value" style="font-size: 19px">
            <el-tag :type="data.player.banned ? 'danger' : 'success'">
              {{ data.player.banned ? '已封禁' : '正常' }}
            </el-tag>
          </div>
        </div>
        <div class="stat">
          <div class="label">注册时间</div>
          <div class="value" style="font-size: 15px">{{ fmtTime(data.player.created_at) }}</div>
        </div>
      </div>

      <div class="panel" style="margin-bottom: 20px">
        <div style="font-weight: 700; margin-bottom: 14px">管理操作</div>
        <div class="toolbar" style="margin-bottom: 0">
          <el-button type="warning" @click="onResetPassword">重置密码</el-button>
          <el-select v-model="currencyForm.currency" style="width: 130px">
            <el-option label="Credits" value="credits" />
            <el-option label="Warfunds" value="warfunds" />
            <el-option label="Gold" value="gold" />
          </el-select>
          <el-input-number v-model="currencyForm.delta" :step="100" />
          <el-button type="primary" @click="onAdjustCurrency">调整货币</el-button>
          <el-button :type="data.player.banned ? 'success' : 'danger'" @click="onToggleBan">
            {{ data.player.banned ? '解除封禁' : '封禁账号' }}
          </el-button>
        </div>
      </div>

      <div class="panel" style="margin-bottom: 20px">
        <div style="font-weight: 700; margin-bottom: 12px">
          角色（{{ data.characters.length }}）
        </div>
        <el-table v-if="data.characters.length" :data="data.characters" size="small">
          <el-table-column label="" width="70">
            <template #default="{ row }">
              <img class="rank-badge" :src="rankIcon(row)" :alt="`军衔 ${row.rank}`"
                   @error="onImgError" />
            </template>
          </el-table-column>
          <el-table-column prop="name" label="角色名" min-width="140" />
          <el-table-column prop="class_name" label="兵种" min-width="150" />
          <el-table-column prop="faction_name" label="阵营" width="110" />
          <el-table-column prop="rank" label="军衔" width="70" />
          <el-table-column label="战斗经验" width="110" align="right">
            <template #default="{ row }">{{ fmtNum(row.combat_xp) }}</template>
          </el-table-column>
          <el-table-column label="战斗徽章" min-width="200">
            <template #default="{ row }">
              <span v-if="row.badge_0 || row.badge_1">
                {{ [row.badge_0, row.badge_1].filter(Boolean).join(' / ') }}
              </span>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="操作" width="100" fixed="right">
            <template #default="{ row }">
              <RouterLink :to="`/players/${playerId}/characters/${row.id}`">
                <el-button size="small" text type="primary">装备明细</el-button>
              </RouterLink>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="该玩家暂无角色" :image-size="60" />
      </div>

      <div class="panel">
        <div style="font-weight: 700; margin-bottom: 12px">最近战报</div>
        <el-table v-if="data.recent_battles.length" :data="data.recent_battles" size="small">
          <el-table-column prop="id" label="战报 ID" width="90" />
          <el-table-column prop="mission_id" label="任务 ID" width="90" />
          <el-table-column prop="winner_faction_id" label="获胜阵营" width="100" />
          <el-table-column label="战斗开始" min-width="165">
            <template #default="{ row }">{{ fmtTime(row.battle_start_time) }}</template>
          </el-table-column>
          <el-table-column label="记录时间" min-width="165">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无战报" :image-size="60" />
      </div>
    </template>
  </div>
</template>
