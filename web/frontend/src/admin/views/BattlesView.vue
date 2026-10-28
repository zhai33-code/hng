<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { fmtTime, fmtNum } from '../format'

interface CharacterEarn {
  character_id: number
  name: string
  class_name: string
  xp_earned: number
  credits: number
  warfunds: number
}

interface BattlePlayer {
  player_id: number
  gamertag: string
  squad_name: string
  score: number
  captures: number
  kills: number
  deaths: number
  is_bot: boolean
  characters: CharacterEarn[]
}

interface BattleRow {
  id: number
  mission_session_id: number
  mission_id: number
  mission_preset_id: number
  preset_name: string
  mode_name: string
  map_name: string
  winner_faction_id: number
  winner_faction: string
  defender_faction_id: number
  defender_faction: string
  mission_time_seconds: number
  battle_start_at: string | null
  created_at: string
}

interface BattleDetail extends BattleRow {
  players: BattlePlayer[]
  capturepoints: { id: number; owner: number }[]
  capture_owner_counts: Record<string, number>
}

const loading = ref(true)
const items = ref<BattleRow[]>([])
const total = ref(0)
const page = reactive({ page: 1, page_size: 20 })

// 详情抽屉
const drawer = ref(false)
const detailLoading = ref(false)
const detail = ref<BattleDetail | null>(null)

async function load() {
  loading.value = true
  try {
    const r = await get<{ total: number; items: BattleRow[] }>(
      `/battles?page=${page.page}&page_size=${page.page_size}`)
    total.value = r.total
    items.value = r.items
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function openDetail(row: BattleRow) {
  drawer.value = true
  detailLoading.value = true
  try {
    detail.value = await get<BattleDetail>(`/battles/${row.id}`)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
    drawer.value = false
  } finally {
    detailLoading.value = false
  }
}

function fmtDuration(sec: number | null): string {
  if (!sec) return '-'
  const m = Math.floor(sec / 60)
  return `${m} 分 ${sec % 60} 秒`
}

function factionTag(id: number): 'success' | 'warning' | 'danger' | undefined {
  return id === 1 ? 'success' : id === 2 ? 'warning' : id === 3 ? 'danger' : undefined
}

function cpsText(d: BattleDetail | null): string {
  if (!d) return '-'
  const entries = Object.entries(d.capture_owner_counts)
  if (!entries.length) return '无据点数据'
  return entries.map(([k, v]) => `${k} ${v}`).join(' / ')
}

onMounted(load)
</script>

<template>
  <div class="panel">
    <el-table v-loading="loading" :data="items" size="small">
      <el-table-column prop="id" label="战报 ID" width="90" />
      <el-table-column label="模式" width="120">
        <template #default="{ row }">{{ row.mode_name || row.preset_name }}</template>
      </el-table-column>
      <el-table-column label="地图" min-width="130">
        <template #default="{ row }">{{ row.map_name || '-' }}</template>
      </el-table-column>
      <el-table-column prop="mission_id" label="任务 ID" width="90" />
      <el-table-column label="获胜阵营" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.winner_faction" :type="factionTag(row.winner_faction_id)" size="small">
            {{ row.winner_faction }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="防守阵营" width="110">
        <template #default="{ row }">
          <el-tag v-if="row.defender_faction" :type="factionTag(row.defender_faction_id)" size="small">
            {{ row.defender_faction }}
          </el-tag>
          <span v-else>-</span>
        </template>
      </el-table-column>
      <el-table-column label="战斗时长" min-width="110">
        <template #default="{ row }">{{ fmtDuration(row.mission_time_seconds) }}</template>
      </el-table-column>
      <el-table-column label="战斗开始" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.battle_start_at) }}</template>
      </el-table-column>
      <el-table-column label="记录时间" min-width="165">
        <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="80" fixed="right">
        <template #default="{ row }">
          <el-button size="small" text type="primary" @click="openDetail(row)">详情</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pager">
      <el-pagination v-model:current-page="page.page" v-model:page-size="page.page_size"
                     :total="total" :page-sizes="[20, 50, 100]"
                     layout="total, sizes, prev, pager, next" @change="load" />
    </div>

    <el-drawer v-model="drawer" size="72%" :title="detail ? `战报 #${detail.id} 详情` : '战报详情'">
      <div v-loading="detailLoading">
        <template v-if="detail">
          <el-descriptions :column="3" border size="small" style="margin-bottom: 16px">
            <el-descriptions-item label="模式 / 地图">
              {{ detail.mode_name }} / {{ detail.map_name }}
            </el-descriptions-item>
            <el-descriptions-item label="官方名称">{{ detail.preset_name }}</el-descriptions-item>
            <el-descriptions-item label="战斗会话">{{ detail.mission_session_id }}</el-descriptions-item>
            <el-descriptions-item label="获胜阵营">
              <el-tag v-if="detail.winner_faction" :type="factionTag(detail.winner_faction_id)" size="small">
                {{ detail.winner_faction }}
              </el-tag>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item label="防守阵营">
              <el-tag v-if="detail.defender_faction" :type="factionTag(detail.defender_faction_id)" size="small">
                {{ detail.defender_faction }}
              </el-tag>
              <span v-else>-</span>
            </el-descriptions-item>
            <el-descriptions-item label="战斗时长">{{ fmtDuration(detail.mission_time_seconds) }}</el-descriptions-item>
            <el-descriptions-item label="战斗开始">{{ fmtTime(detail.battle_start_at) }}</el-descriptions-item>
            <el-descriptions-item label="记录时间">{{ fmtTime(detail.created_at) }}</el-descriptions-item>
            <el-descriptions-item label="据点终态">{{ cpsText(detail) }}</el-descriptions-item>
          </el-descriptions>

          <div style="font-weight: 700; margin-bottom: 10px">
            参战玩家（{{ detail.players.length }}，含 AI）
          </div>
          <el-table v-if="detail.players.length" :data="detail.players" size="small">
            <el-table-column type="expand">
              <template #default="{ row }">
                <el-table v-if="row.characters.length" :data="row.characters" size="small"
                          style="padding: 4px 24px">
                  <el-table-column prop="name" label="角色名" min-width="130">
                    <template #default="{ row: c }">{{ c.name || `#${c.character_id}` }}</template>
                  </el-table-column>
                  <el-table-column prop="class_name" label="兵种" min-width="120" />
                  <el-table-column label="获得 XP" width="110" align="right">
                    <template #default="{ row: c }">{{ fmtNum(c.xp_earned) }}</template>
                  </el-table-column>
                  <el-table-column label="Credits" width="110" align="right">
                    <template #default="{ row: c }">{{ fmtNum(c.credits) }}</template>
                  </el-table-column>
                  <el-table-column label="Warfunds" width="110" align="right">
                    <template #default="{ row: c }">{{ fmtNum(c.warfunds) }}</template>
                  </el-table-column>
                </el-table>
                <div v-else style="padding: 8px 24px; color: #8a9360">无兵种收益数据</div>
              </template>
            </el-table-column>
            <el-table-column label="玩家" min-width="160">
              <template #default="{ row }">
                <RouterLink v-if="!row.is_bot && row.player_id"
                            :to="`/players/${row.player_id}`" class="player-link">
                  {{ row.gamertag || `#${row.player_id}` }}
                </RouterLink>
                <span v-else>{{ row.gamertag || `#${row.player_id}` }}
                  <el-tag size="small" type="info">AI</el-tag>
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="squad_name" label="小队" min-width="130">
              <template #default="{ row }">{{ row.squad_name || '-' }}</template>
            </el-table-column>
            <el-table-column prop="score" label="得分" width="90" align="right">
              <template #default="{ row }">{{ fmtNum(row.score) }}</template>
            </el-table-column>
            <el-table-column prop="kills" label="击杀" width="80" align="right" />
            <el-table-column prop="deaths" label="死亡" width="80" align="right" />
            <el-table-column prop="captures" label="占点" width="80" align="right" />
          </el-table>
          <el-empty v-else description="无参战玩家数据" :image-size="60" />
        </template>
      </div>
    </el-drawer>
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
