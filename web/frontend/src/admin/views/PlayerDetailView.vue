<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'
import { fmtTime, fmtNum } from '../format'

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
  badge_0_id: number
  badge_1_id: number
  created_at: string
}

interface PlayerDetail {
  player: {
    id: number
    username: string
    credits: number
    warfunds: number
    gold: number
    total_score: number
    level: number
    banned: boolean
    created_at: string
  }
  characters: CharacterRow[]
  recent_battles: {
    id: number
    mission_id: number
    mode_name: string
    map_name: string
    preset_name: string
    winner_faction_id: number
    winner_faction: string
    battle_start_at: string | null
    created_at: string
    own: { score: number; kills: number; deaths: number; captures: number }
  }[]
}

const route = useRoute()
const playerId = computed(() => Number(route.params.id))
const loading = ref(true)
const data = ref<PlayerDetail | null>(null)

// ---- 阵营分组（tab）----
const activeFaction = ref<string>('')
/** 按 faction id 升序分组（1=US/2=GE/3=SU），tab 内不再显示阵营列 */
const factionGroups = computed(() => {
  const chars = data.value?.characters ?? []
  const groups: { faction: number; name: string; characters: CharacterRow[] }[] = []
  for (const c of [...chars].sort((a, b) => a.faction - b.faction)) {
    const last = groups[groups.length - 1]
    if (last && last.faction === c.faction) {
      last.characters.push(c)
    } else {
      groups.push({ faction: c.faction, name: c.faction_name, characters: [c] })
    }
  }
  return groups
})

async function load() {
  loading.value = true
  try {
    data.value = await get<PlayerDetail>(`/players/${playerId.value}`)
    // 默认选中第一个阵营 tab（当前选中阵营不存在时重置）
    const ids = factionGroups.value.map((g) => String(g.faction))
    if (!ids.includes(activeFaction.value)) {
      activeFaction.value = ids[0] ?? ''
    }
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}
onMounted(load)

/** 军衔徽章：客户端 images/Rank/{faction}_{branch}_{rank}.png（找不到则隐藏） */
function rankIcon(c: CharacterRow): string {
  return `/icons/Rank/${c.faction}_1_${c.rank}.png`
}
/** 战斗徽章：客户端 images/CombatBadges/{badgeTemplate id}.png（无图或 0 则只显示文字） */
function badgeSrc(id: number): string {
  return id ? `/icons/CombatBadges/${id}.png` : ''
}
function onImgError(e: Event) {
  ;(e.target as HTMLElement).style.display = 'none'
}

// ---- 重置密码 ----
async function onResetPassword() {
  let pw: string
  try {
    const r = await ElMessageBox.prompt(
      '输入新密码（6-64 位，明文写入数据库，与游戏服存储方式一致）',
      '重置玩家密码', { inputType: 'password', inputPattern: /^.{6,64}$/,
        inputErrorMessage: '密码长度需为 6-64 位' })
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

// ---- 经验调整（账号经验 totalScore → 进展 tab 等级）----
const scoreForm = reactive({ delta: 1000 })

async function onAdjustScore() {
  const delta = Number(scoreForm.delta)
  const cur = data.value?.player
  try {
    await ElMessageBox.confirm(
      `确认对玩家 ${cur?.username} ${delta >= 0 ? '增加' : '扣减'} ` +
      `${Math.abs(delta)} 经验？（当前 ${fmtNum(cur?.total_score ?? 0)} 分，` +
      `等级 Lv.${cur?.level ?? 0}；奖励弹窗将在玩家下次登录/战局结算时补发）`,
      '调整经验', { type: 'warning' })
  } catch {
    return
  }
  try {
    const r = await post<{ ok: boolean; total_score: number; level: number }>(
      `/players/${playerId.value}/score`, { delta },
    )
    ElMessage.success(`已调整，当前经验 ${fmtNum(r.total_score)}，等级 Lv.${r.level}`)
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
          <div class="label">账号等级</div>
          <div class="value num">Lv.{{ data.player.level }}</div>
        </div>
        <div class="stat">
          <div class="label">账号经验</div>
          <div class="value num" :class="{ long: data.player.total_score >= 10_000_000 }">{{ fmtNum(data.player.total_score) }}</div>
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
          <el-input-number v-model="scoreForm.delta" :step="100" />
          <el-button type="primary" @click="onAdjustScore">调整经验</el-button>
          <el-button :type="data.player.banned ? 'success' : 'danger'" @click="onToggleBan">
            {{ data.player.banned ? '解除封禁' : '封禁账号' }}
          </el-button>
        </div>
      </div>

      <div class="panel" style="margin-bottom: 20px">
        <div style="font-weight: 700; margin-bottom: 12px">
          角色（{{ data.characters.length }}）
        </div>
        <el-tabs v-if="factionGroups.length" v-model="activeFaction">
          <el-tab-pane v-for="g in factionGroups" :key="g.faction" :name="String(g.faction)">
            <template #label>
              {{ g.name }}（{{ g.characters.length }}）
            </template>
            <el-table :data="g.characters" size="small">
              <el-table-column label="" width="100">
                <template #default="{ row }">
                  <img class="rank-badge" :src="rankIcon(row)" :alt="`军衔 ${row.rank}`"
                       @error="onImgError" />
                </template>
              </el-table-column>
              <el-table-column prop="name" label="角色名" />
              <el-table-column prop="class_name" label="兵种" />
              <el-table-column prop="rank" label="军衔" />
              <el-table-column label="战斗经验" >
                <template #default="{ row }">{{ fmtNum(row.combat_xp) }}</template>
              </el-table-column>
              <el-table-column label="战斗徽章" >
                <template #default="{ row }">
                  <!-- 图标在上、文字在下 -->
                  <div v-if="row.badge_0 || row.badge_1" class="badges">
                    <div v-for="b in [{ id: row.badge_0_id, name: row.badge_0 },
                                      { id: row.badge_1_id, name: row.badge_1 }].filter(b => b.name)"
                         :key="b.id" class="badge-item">
                      <img v-if="badgeSrc(b.id)" class="combat-badge" :src="badgeSrc(b.id)"
                           :alt="b.name" :title="b.name" @error="onImgError" />
                      <span class="badge-name">{{ b.name }}</span>
                    </div>
                  </div>
                  <span v-else>-</span>
                </template>
              </el-table-column>
              <el-table-column label="操作" fixed="right">
                <template #default="{ row }">
                  <RouterLink :to="`/players/${playerId}/characters/${row.id}`">
                    <el-button size="small" text type="primary">装备明细</el-button>
                  </RouterLink>
                </template>
              </el-table-column>
            </el-table>
          </el-tab-pane>
        </el-tabs>
        <el-empty v-else description="该玩家暂无角色" :image-size="60" />
      </div>

      <div class="panel">
        <div style="font-weight: 700; margin-bottom: 12px">最近参战（{{ data.recent_battles.length }}）</div>
        <el-table v-if="data.recent_battles.length" :data="data.recent_battles" size="small">
          <el-table-column prop="id" label="战报 ID" width="90" />
          <el-table-column label="模式 / 地图" min-width="180">
            <template #default="{ row }">
              {{ row.mode_name || row.preset_name }} / {{ row.map_name || '-' }}
            </template>
          </el-table-column>
          <el-table-column prop="mission_id" label="任务 ID" width="90" />
          <el-table-column label="获胜阵营" width="110">
            <template #default="{ row }">{{ row.winner_faction || '-' }}</template>
          </el-table-column>
          <el-table-column label="本人得分" width="90" align="right">
            <template #default="{ row }">{{ fmtNum(row.own.score) }}</template>
          </el-table-column>
          <el-table-column label="击杀/死亡" width="100" align="center">
            <template #default="{ row }">{{ row.own.kills }} / {{ row.own.deaths }}</template>
          </el-table-column>
          <el-table-column label="占点" width="70" align="right">
            <template #default="{ row }">{{ row.own.captures }}</template>
          </el-table-column>
          <el-table-column label="战斗开始" min-width="165">
            <template #default="{ row }">{{ fmtTime(row.battle_start_at) }}</template>
          </el-table-column>
          <el-table-column label="记录时间" min-width="165">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无参战记录" :image-size="60" />
      </div>
    </template>
  </div>
</template>
