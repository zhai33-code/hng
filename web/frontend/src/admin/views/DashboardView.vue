<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface DashboardData {
  players: number
  today_registrations: number
  characters: number
  battles: number
  recent_players: { id: number; username: string; created_at: string }[]
  wardb_loaded: number
}

interface RegStat {
  days: number
  items: { day: string; count: number }[]
}

interface OnlineSnapshot {
  online: number
  in_queue: number
  in_battle: number
  players: { username: string; player_id: number | null }[]
  missions: {
    mission_id: number
    preset: string | null
    gamemode: number | null
    attacker_faction: number | null
    defender_faction: number | null
    players: { gamertag: string | null; faction: number | null }[]
  }[]
  server_time: number
}

const loading = ref(true)
const data = ref<DashboardData | null>(null)
const regStat = ref<RegStat | null>(null)
const online = ref<OnlineSnapshot | null>(null)
const onlineUnreachable = ref(false)

const ONLINE_REFRESH_MS = 30_000
let onlineTimer: ReturnType<typeof setInterval> | null = null

async function loadOnline(initial = false) {
  try {
    online.value = await get<OnlineSnapshot>('/online')
    onlineUnreachable.value = false
  } catch (e: unknown) {
    onlineUnreachable.value = true
    // 首次加载才弹错误提示，定时轮询失败只静默标记（避免刷屏）
    if (initial) ElMessage.error((e as Error).message || '在线统计不可用')
  }
}

onMounted(async () => {
  try {
    const [d, s] = await Promise.all([
      get<DashboardData>('/dashboard'),
      get<RegStat>('/stats/registrations?days=30'),
    ])
    data.value = d
    regStat.value = s
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
  await loadOnline(true)
  onlineTimer = setInterval(() => loadOnline(), ONLINE_REFRESH_MS)
})

onBeforeUnmount(() => {
  if (onlineTimer) clearInterval(onlineTimer)
})

function fmtTime(s: string | null): string {
  return s ? s.replace('T', ' ').slice(0, 19) : '-'
}

const FACTION_NAMES: Record<number, string> = { 1: '美', 2: '德', 3: '苏' }
const factionName = (f: number | null | undefined) =>
  (f != null && FACTION_NAMES[f]) || '-'

// 简易柱状图（纯 CSS，避免引图表库）
const maxCount = () =>
  Math.max(1, ...(regStat.value?.items.map((i) => i.count) ?? [1]))
</script>

<template>
  <div v-loading="loading">
    <div class="stat-cards">
      <div class="stat online-stat">
        <div class="label">当前在线</div>
        <div class="value">{{ onlineUnreachable ? '不可用' : (online?.online ?? '-') }}</div>
      </div>
      <div class="stat">
        <div class="label">战斗中</div>
        <div class="value">{{ onlineUnreachable ? '-' : (online?.in_battle ?? '-') }}</div>
      </div>
      <div class="stat">
        <div class="label">匹配排队</div>
        <div class="value">{{ onlineUnreachable ? '-' : (online?.in_queue ?? '-') }}</div>
      </div>
      <div class="stat">
        <div class="label">玩家总数</div>
        <div class="value">{{ data?.players ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">今日注册</div>
        <div class="value">{{ data?.today_registrations ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">角色总数</div>
        <div class="value">{{ data?.characters ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">战斗场次</div>
        <div class="value">{{ data?.battles ?? '-' }}</div>
      </div>
      <div class="stat">
        <div class="label">WarDatabase 模板</div>
        <div class="value">{{ data?.wardb_loaded ?? '-' }}</div>
      </div>
    </div>

    <div v-if="onlineUnreachable" class="panel" style="margin-bottom: 20px">
      <div style="font-weight: 700; margin-bottom: 8px">实时在线</div>
      <el-empty description="游戏服在线统计不可达（服务未启动或版本过旧）" :image-size="60" />
    </div>
    <div v-else class="panel" style="margin-bottom: 20px">
      <div style="font-weight: 700; margin-bottom: 12px">
        实时在线（{{ online?.online ?? 0 }} 人 · 战斗中 {{ online?.in_battle ?? 0 }} ·
        排队 {{ online?.in_queue ?? 0 }}，每 30 秒自动刷新）
      </div>
      <el-table v-if="online?.players.length" :data="online.players" size="small">
        <el-table-column prop="username" label="用户名" min-width="160" />
        <el-table-column label="玩家 ID" width="110">
          <template #default="{ row }">{{ row.player_id ?? '-' }}</template>
        </el-table-column>
      </el-table>
      <el-empty v-else description="当前无玩家在线" :image-size="60" />
      <template v-if="online?.missions.length">
        <div style="font-weight: 700; margin: 16px 0 8px">活跃战局</div>
        <el-table :data="online.missions" size="small">
          <el-table-column prop="mission_id" label="战局 ID" width="100" />
          <el-table-column prop="preset" label="预设" min-width="120" />
          <el-table-column label="攻/守阵营" width="110">
            <template #default="{ row }">
              {{ factionName(row.attacker_faction) }} / {{ factionName(row.defender_faction) }}
            </template>
          </el-table-column>
          <el-table-column label="参战玩家" min-width="220">
            <template #default="{ row }">
              <span v-for="p in row.players" :key="p.gamertag" class="mission-player">
                {{ p.gamertag }}（{{ factionName(p.faction) }}）
              </span>
            </template>
          </el-table-column>
        </el-table>
      </template>
    </div>

    <div class="panel" style="margin-bottom: 20px">
      <div style="font-weight: 700; margin-bottom: 12px">近 30 天注册趋势</div>
      <div v-if="regStat && regStat.items.length" class="chart">
        <div v-for="it in regStat.items" :key="it.day" class="chart-col"
             :title="`${it.day}: ${it.count}`">
          <div class="chart-bar" :style="{ height: `${(it.count / maxCount()) * 100}%` }" />
          <div class="chart-label">{{ it.day.slice(5) }}</div>
        </div>
      </div>
      <el-empty v-else description="暂无注册数据" :image-size="60" />
    </div>

    <div class="panel">
      <div style="font-weight: 700; margin-bottom: 12px">最近注册玩家</div>
      <el-table :data="data?.recent_players ?? []" size="small">
        <el-table-column prop="id" label="ID" width="80" />
        <el-table-column prop="username" label="用户名" min-width="160" />
        <el-table-column label="注册时间" min-width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
    </div>
  </div>
</template>

<style lang="scss" scoped>
.online-stat .value {
  color: #a7c070;
}

.mission-player {
  display: inline-block;
  margin: 0 8px 2px 0;
  white-space: nowrap;
}

.chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 140px;
  padding-top: 8px;

  .chart-col {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: flex-end;
    height: 100%;

    .chart-bar {
      background: linear-gradient(180deg, #8b9a46, #55632b);
      border-radius: 2px 2px 0 0;
      min-height: 2px;
    }

    .chart-label {
      font-size: 9px;
      color: #7a7561;
      text-align: center;
      margin-top: 4px;
      white-space: nowrap;
      overflow: hidden;
    }
  }
}
</style>
