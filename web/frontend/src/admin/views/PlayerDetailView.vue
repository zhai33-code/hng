<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
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
    ban_reason: string
    ban_expires: string | null
    created_at: string
  }
  characters: CharacterRow[]
  machine_bans: { machine_id: string; reason: string; created_at: string }[]
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
  login_events: LoginEventRow[]
  linked_accounts: { same_machine: string[]; same_ip: string[] }
}

const route = useRoute()
const router = useRouter()
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
const banDialog = ref(false)
const banForm = reactive({
  type: 'permanent' as 'permanent' | 'temporary',
  duration: 24,          // 小时；0 = 自定义时间
  customTime: '',
  reason: '',
  banMachines: false,
})
const banDurations = [
  { label: '1 小时', value: 1 },
  { label: '6 小时', value: 6 },
  { label: '12 小时', value: 12 },
  { label: '1 天', value: 24 },
  { label: '3 天', value: 72 },
  { label: '7 天', value: 168 },
  { label: '14 天', value: 336 },
  { label: '30 天', value: 720 },
  { label: '自定义时间', value: 0 },
]

/** 限时封禁到期时刻（ISO 8601 带时区，传给后端校验） */
const banExpires = computed<string | null>(() => {
  if (banForm.type !== 'temporary') return null
  if (banForm.duration > 0)
    return new Date(Date.now() + banForm.duration * 3_600_000).toISOString()
  return banForm.customTime
    ? new Date(banForm.customTime).toISOString()
    : null
})

function onOpenBanDialog() {
  banForm.type = 'permanent'
  banForm.duration = 24
  banForm.customTime = ''
  banForm.reason = ''
  banForm.banMachines = false
  banDialog.value = true
}

async function onConfirmBan() {
  if (banForm.type === 'temporary' && !banExpires.value) {
    ElMessage.warning('请选择封禁到期时间')
    return
  }
  try {
    await post(`/players/${playerId.value}/ban`, {
      banned: true,
      reason: banForm.reason,
      expires_at: banExpires.value,
      ban_machines: banForm.banMachines,
    })
    ElMessage.success(banForm.type === 'permanent' ? '已永久封禁' : '已限时封禁')
    banDialog.value = false
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

async function onUnban() {
  try {
    await ElMessageBox.confirm(
      `确认解除玩家 ${data.value?.player.username} 的封禁？`,
      '解除封禁', { type: 'warning' })
  } catch {
    return
  }
  try {
    await post(`/players/${playerId.value}/ban`, { banned: false })
    ElMessage.success('已解封')
    load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

/** 封禁状态标签 */
const banStatus = computed(() => {
  const p = data.value?.player
  if (!p?.banned) return { text: '正常', type: 'success' as const }
  if (p.ban_expires) {
    if (new Date(p.ban_expires).getTime() <= Date.now())
      return { text: '封禁已到期', type: 'warning' as const }
    return { text: `限时至 ${fmtTime(p.ban_expires)}`, type: 'danger' as const }
  }
  return { text: '永久封禁', type: 'danger' as const }
})

// ---- 登录记录 ----
const loginResultText: Record<number, string> = {
  97: '成功', 98: '密码错误', 91: '封禁拦截(永久)', 92: '封禁拦截(限时)',
}
const loginSourceText: Record<string, string> = {
  game: '游戏登录', register: '注册', import: '迁移',
}

/** 机器码/IP 跳全局反查页（q= 搜索词） */
function searchLogins(q: string) {
  router.push({ name: 'login-events', query: { q } })
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
          <div class="value" style="font-size: 15px">
            <el-tag :type="banStatus.type">
              {{ banStatus.text }}
            </el-tag>
            <div v-if="data.player.banned && data.player.ban_reason"
                 style="font-size: 13px; margin-top: 6px; color: #909399">
              原因：{{ data.player.ban_reason }}
            </div>
            <div v-if="data.machine_bans.length"
                 style="font-size: 13px; margin-top: 6px; color: #e6a23c">
              连带机器码封禁 {{ data.machine_bans.length }} 台
            </div>
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
          <el-button v-if="data.player.banned" type="success" @click="onUnban">
            解除封禁
          </el-button>
          <el-button v-else type="danger" @click="onOpenBanDialog">封禁账号</el-button>
        </div>
      </div>

      <div class="panel" style="margin-bottom: 20px">
        <div style="font-weight: 700; margin-bottom: 12px">
          登录记录（{{ data.login_events.length }}）
          <el-tag v-if="data.linked_accounts.same_machine.length" type="danger" style="margin-left: 10px">
            同机器码登录过 {{ data.linked_accounts.same_machine.length }} 个其他账号
          </el-tag>
          <el-tag v-else-if="data.linked_accounts.same_ip.length" type="warning" style="margin-left: 10px">
            同 IP 登录过 {{ data.linked_accounts.same_ip.length }} 个其他账号
          </el-tag>
        </div>
        <div v-if="data.linked_accounts.same_machine.length || data.linked_accounts.same_ip.length"
             style="margin-bottom: 12px; line-height: 2">
          <template v-if="data.linked_accounts.same_machine.length">
            <strong>同机器码：</strong>
            <el-tag v-for="u in data.linked_accounts.same_machine" :key="'m' + u" type="danger"
                    style="margin-right: 6px" class="link-account">{{ u }}</el-tag>
          </template>
          <template v-if="data.linked_accounts.same_ip.length">
            <strong>同 IP：</strong>
            <el-tag v-for="u in data.linked_accounts.same_ip" :key="'i' + u" type="warning"
                    style="margin-right: 6px" class="link-account">{{ u }}</el-tag>
          </template>
        </div>
        <el-table v-if="data.login_events.length" :data="data.login_events" size="small">
          <el-table-column label="时间" min-width="165">
            <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
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
              <el-link v-if="row.ip" type="primary" @click="searchLogins(row.ip)">{{ row.ip }}</el-link>
              <span v-else>-</span>
            </template>
          </el-table-column>
          <el-table-column label="机器码" min-width="280">
            <template #default="{ row }">
              <el-link v-if="row.machine_id" type="primary" @click="searchLogins(row.machine_id)">
                {{ row.machine_id }}
              </el-link>
              <span v-else>-</span>
            </template>
          </el-table-column>
        </el-table>
        <el-empty v-else description="暂无登录记录" :image-size="60" />
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

      <el-dialog v-model="banDialog" title="封禁账号" width="500px">
        <el-form label-width="90px">
          <el-form-item label="封禁类型">
            <el-radio-group v-model="banForm.type">
              <el-radio value="permanent">永久封禁</el-radio>
              <el-radio value="temporary">限时封禁</el-radio>
            </el-radio-group>
          </el-form-item>
          <template v-if="banForm.type === 'temporary'">
            <el-form-item label="封禁时长">
              <el-select v-model="banForm.duration" style="width: 200px">
                <el-option v-for="d in banDurations" :key="d.value"
                           :label="d.label" :value="d.value" />
              </el-select>
            </el-form-item>
            <el-form-item v-if="banForm.duration === 0" label="到期时间">
              <el-date-picker v-model="banForm.customTime" type="datetime"
                              placeholder="选择封禁到期时间" />
            </el-form-item>
          </template>
          <el-form-item label="封禁原因">
            <el-input v-model="banForm.reason" type="textarea" :rows="2"
                      maxlength="200" show-word-limit
                      placeholder="登录弹窗展示给玩家（可留空）" />
          </el-form-item>
          <el-form-item label="连带机器">
            <el-checkbox v-model="banForm.banMachines">
              连带封禁该玩家登录过的机器码（客户端启动即拦截，防小号）
            </el-checkbox>
          </el-form-item>
        </el-form>
        <div style="color: #909399; font-size: 13px; line-height: 1.8">
          封禁立即生效：在线连接会被踢下线，登录弹窗展示上述类型与原因；
          限时封禁到期后自动解除。
        </div>
        <template #footer>
          <el-button @click="banDialog = false">取消</el-button>
          <el-button type="danger" @click="onConfirmBan">确认封禁</el-button>
        </template>
      </el-dialog>
    </template>
  </div>
</template>
