<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post, http } from '../api'
import { fmtTime } from '../format'

interface WarRow {
  id: number
  name: string
  begintime: string
  endtime: string | null
  winning_faction_id: number
  winning_faction: string
  points_needed_for_victory: number
  sequel_war_id: number
  state: number
}

interface AtStat {
  total: number
  idle: number
  moving: number
  battle: number
}

interface Overview {
  rts_ready: boolean
  war: {
    id: number
    name: string
    begintime: string
    endtime: string | null
    points_needed_for_victory: number
    state: number
  } | null
  city_counts: Record<string, number>
  at_stats: Record<string, AtStat>
  active_movements: number
  active_battles: number
  history: WarRow[]
}

interface AtRow {
  id: number
  name: string
  template_id: number
  faction: number
  owner_player_id: number
  owner_name: string
  upgrade_level: number
  morale: number
  max_morale: number
  state: number
  state_name: string
  map_entity_id: number
  map_entity_type_name: string
  created_at: string
}

interface MovementRow {
  id: number
  commandnode_id: number
  at_name: string
  faction: number
  dest_bf: number
  dest_ap: number
  segments: number
  started_at: string
  arrive_at: string
}

interface WarBattleRow {
  id: number
  war_id: number
  battlefield_id: number
  factions: { at_id: number; faction: number }[]
  state: number
  started_at: string
  ended_at: string | null
}

interface CityRow {
  battlefield_id: number
  owner_faction: number
  owner_name: string
}

const overview = ref<Overview | null>(null)
const loading = ref(true)

// AT 列表过滤 + 分页
const atFilter = reactive({ faction: 0, state: -1 })
const atPage = reactive({ page: 1, page_size: 20 })
const atTotal = ref(0)
const ats = ref<AtRow[]>([])
const atLoading = ref(false)

const movements = ref<MovementRow[]>([])
const battles = ref<WarBattleRow[]>([])
const cities = ref<CityRow[]>([])
const tab = ref('ats')

const FACTION_OPTIONS = [
  { label: '全部阵营', value: 0 },
  { label: 'US', value: 1 },
  { label: 'GE', value: 2 },
  { label: 'SU', value: 3 },
]
const AT_STATE_OPTIONS = [
  { label: '全部存活', value: -1 },
  { label: '待命', value: 0 },
  { label: '移动中', value: 1 },
  { label: '会战中', value: 2 },
]

async function loadOverview() {
  overview.value = await get<Overview>('/war/overview')
}

async function loadAts() {
  atLoading.value = true
  try {
    const r = await get<{ total: number; items: AtRow[] }>(
      `/war/ats?page=${atPage.page}&page_size=${atPage.page_size}` +
      `&faction=${atFilter.faction}&state=${atFilter.state}`)
    atTotal.value = r.total
    ats.value = r.items
  } finally {
    atLoading.value = false
  }
}

async function loadMovements() {
  const r = await get<{ items: MovementRow[] }>('/war/movements')
  movements.value = r.items
}

async function loadBattles() {
  const r = await get<{ items: WarBattleRow[] }>('/war/battles')
  battles.value = r.items
}

async function loadCities() {
  const r = await get<{ items: CityRow[] }>('/war/cities')
  cities.value = r.items
}

async function load() {
  loading.value = true
  try {
    await Promise.all([loadOverview(), loadAts(), loadMovements(), loadBattles(), loadCities()])
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

function factionTag(id: number): 'success' | 'warning' | 'danger' | undefined {
  return id === 1 ? 'success' : id === 2 ? 'warning' : id === 3 ? 'danger' : undefined
}

function factionShort(id: number): string {
  return id === 1 ? 'US' : id === 2 ? 'GE' : id === 3 ? 'SU' : '?'
}

function onTab(name: string | number) {
  if (name === 'ats') loadAts()
}

// ------------------------------------------------------------------ 写操作

async function forceEndWar() {
  const w = overview.value?.war
  if (!w) return
  try {
    await ElMessageBox.confirm(
      `确定强制结束战争 #${w.id}（${w.name || '未命名'}）？` +
      '游戏服下个 tick 会自动开新战（MM_WAR_AUTO_CREATE=1 时）。',
      '强制结束战争', { type: 'warning' })
  } catch {
    return
  }
  try {
    await post('/war/end')
    ElMessage.success('战争已结束')
    await load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '操作失败')
  }
}

// 生成 AT 对话框
const createDlg = ref(false)
const createLoading = ref(false)
const createForm = reactive({ owner_player_id: 0, template_id: 0, faction: 1, name: '' })

function openCreateDlg() {
  createForm.owner_player_id = 0
  createForm.template_id = 0
  createForm.faction = 1
  createForm.name = ''
  createDlg.value = true
}

async function submitCreateAt() {
  if (!createForm.owner_player_id || !createForm.template_id) {
    ElMessage.warning('需要玩家 ID 与模板 ID')
    return
  }
  createLoading.value = true
  try {
    const r = await post<{ id: number }>('/war/ats', { ...createForm })
    ElMessage.success(`AT #${r.id} 已生成`)
    createDlg.value = false
    await loadAts()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '生成失败')
  } finally {
    createLoading.value = false
  }
}

// 调整 warfunds 对话框
const fundsDlg = ref(false)
const fundsLoading = ref(false)
const fundsForm = reactive({ player_id: 0, delta: 0 })

function openFundsDlg(prefillPid?: number) {
  fundsForm.player_id = prefillPid || 0
  fundsForm.delta = 0
  fundsDlg.value = true
}

async function submitFunds() {
  if (!fundsForm.player_id || !fundsForm.delta) {
    ElMessage.warning('需要玩家 ID 与非零调整值')
    return
  }
  fundsLoading.value = true
  try {
    const r = await post<{ balance: number }>('/war/warfunds', { ...fundsForm })
    ElMessage.success(`调整成功，余额 ${r.balance}`)
    fundsDlg.value = false
    await loadAts()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '调整失败')
  } finally {
    fundsLoading.value = false
  }
}

async function dissolveAt(row: AtRow) {
  try {
    await ElMessageBox.confirm(
      `确定解散 AT #${row.id}（${row.name || '未命名'}）？作弊解散不退款。`,
      '解散 AT', { type: 'warning' })
  } catch {
    return
  }
  try {
    await http('DELETE', `/war/ats/${row.id}`)
    ElMessage.success('已解散')
    await loadAts()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '解散失败')
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="panel">
    <el-alert v-if="overview && !overview.rts_ready" type="info" :closable="false"
              title="RTS 数据表尚未创建（游戏服未启动过战争系统），无数据可展示" />
    <template v-if="overview && overview.rts_ready">
      <!-- 概览 -->
      <el-descriptions v-if="overview.war" :column="4" border size="small" class="war-head">
        <el-descriptions-item label="战争">
          #{{ overview.war.id }} {{ overview.war.name }}
          <el-tag size="small" :type="overview.war.state === 1 ? 'success' : 'info'">
            {{ overview.war.state === 1 ? '进行中' : '已结束' }}
          </el-tag>
        </el-descriptions-item>
        <el-descriptions-item label="开战时间">{{ fmtTime(overview.war.begintime) }}</el-descriptions-item>
        <el-descriptions-item label="结束时间">{{ fmtTime(overview.war.endtime) }}</el-descriptions-item>
        <el-descriptions-item label="胜利点数">{{ overview.war.points_needed_for_victory }}</el-descriptions-item>
        <el-descriptions-item label="运维" width="140">
          <el-button v-if="overview.war.state === 1" size="small" type="danger"
                     plain @click="forceEndWar">强制结束</el-button>
          <el-button size="small" plain @click="openFundsDlg()">调整 Warfunds</el-button>
        </el-descriptions-item>
      </el-descriptions>

      <div class="stat-cards">
        <div v-for="fid in [1, 2, 3]" :key="fid" class="stat-card">
          <el-tag :type="factionTag(fid)" size="small">{{ factionShort(fid) }}</el-tag>
          <div class="stat-line">城市 {{ overview.city_counts[String(fid)] || 0 }}</div>
          <div class="stat-line">
            AT {{ overview.at_stats[String(fid)]?.total || 0 }}
            <span class="stat-sub">
              （待命 {{ overview.at_stats[String(fid)]?.idle || 0 }} /
              移动 {{ overview.at_stats[String(fid)]?.moving || 0 }} /
              会战 {{ overview.at_stats[String(fid)]?.battle || 0 }}）
            </span>
          </div>
        </div>
        <div class="stat-card">
          <span class="stat-title">全局</span>
          <div class="stat-line">在途移动 {{ overview.active_movements }}</div>
          <div class="stat-line">进行中会战 {{ overview.active_battles }}</div>
        </div>
      </div>

      <el-tabs v-model="tab" @tab-change="onTab">
        <!-- AT 部队 -->
        <el-tab-pane label="AT 部队" name="ats">
          <div class="toolbar">
            <el-select v-model="atFilter.faction" size="small" style="width: 120px"
                       @change="() => { atPage.page = 1; loadAts() }">
              <el-option v-for="o in FACTION_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-select v-model="atFilter.state" size="small" style="width: 120px"
                       @change="() => { atPage.page = 1; loadAts() }">
              <el-option v-for="o in AT_STATE_OPTIONS" :key="o.value" :label="o.label" :value="o.value" />
            </el-select>
            <el-button size="small" type="primary" plain @click="openCreateDlg">生成 AT</el-button>
          </div>
          <el-table v-loading="atLoading" :data="ats" size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="name" label="部队名" min-width="150">
              <template #default="{ row }">{{ row.name || `#${row.id}` }}</template>
            </el-table-column>
            <el-table-column label="阵营" width="80">
              <template #default="{ row }">
                <el-tag :type="factionTag(row.faction)" size="small">
                  {{ factionShort(row.faction) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="指挥官" min-width="130">
              <template #default="{ row }">
                <RouterLink v-if="row.owner_player_id" :to="`/players/${row.owner_player_id}`"
                            class="player-link">
                  {{ row.owner_name || `#${row.owner_player_id}` }}
                </RouterLink>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column prop="template_id" label="模板" width="80" />
            <el-table-column prop="upgrade_level" label="升级" width="60" align="right" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.state === 2 ? 'danger' : row.state === 1 ? 'primary' : 'info'">
                  {{ row.state_name }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="位置" min-width="120">
              <template #default="{ row }">
                {{ row.map_entity_type_name }} #{{ row.map_entity_id }}
              </template>
            </el-table-column>
            <el-table-column label="士气" width="100">
              <template #default="{ row }">{{ row.morale }}/{{ row.max_morale }}</template>
            </el-table-column>
            <el-table-column label="创建时间" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
            </el-table-column>
            <el-table-column label="操作" width="150" fixed="right">
              <template #default="{ row }">
                <template v-if="row.state < 3">
                  <el-button size="small" text type="danger" @click="dissolveAt(row)">解散</el-button>
                  <el-button v-if="row.owner_player_id" size="small" text
                             @click="openFundsDlg(row.owner_player_id)">发资金</el-button>
                </template>
              </template>
            </el-table-column>
          </el-table>
          <div class="pager">
            <el-pagination v-model:current-page="atPage.page" v-model:page-size="atPage.page_size"
                           :total="atTotal" :page-sizes="[20, 50, 100]"
                           layout="total, sizes, prev, pager, next" @change="loadAts" />
          </div>
        </el-tab-pane>

        <!-- 在途移动 -->
        <el-tab-pane :label="`在途移动（${movements.length}）`" name="movements">
          <el-table :data="movements" size="small">
            <el-table-column prop="id" label="移动 ID" width="90" />
            <el-table-column prop="at_name" label="部队" min-width="150">
              <template #default="{ row }">{{ row.at_name || `#${row.commandnode_id}` }}</template>
            </el-table-column>
            <el-table-column label="阵营" width="80">
              <template #default="{ row }">
                <el-tag :type="factionTag(row.faction)" size="small">
                  {{ factionShort(row.faction) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="目的地" min-width="110">
              <template #default="{ row }">
                {{ row.dest_ap ? `出兵点 #${row.dest_ap}` : `城市 #${row.dest_bf}` }}
              </template>
            </el-table-column>
            <el-table-column prop="segments" label="段数" width="70" align="right" />
            <el-table-column label="出发时间" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
            </el-table-column>
            <el-table-column label="预计到达" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.arrive_at) }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!movements.length" description="无在途移动" :image-size="60" />
        </el-tab-pane>

        <!-- 会战 -->
        <el-tab-pane :label="`会战（${battles.filter(b => b.state === 0).length} 进行中）`" name="battles">
          <el-table :data="battles" size="small">
            <el-table-column prop="id" label="会战 ID" width="90" />
            <el-table-column prop="war_id" label="战争 ID" width="90" />
            <el-table-column label="城市" width="110">
              <template #default="{ row }">#{{ row.battlefield_id }}</template>
            </el-table-column>
            <el-table-column label="参战 AT" min-width="200">
              <template #default="{ row }">
                <el-tag v-for="f in row.factions" :key="f.at_id" :type="factionTag(f.faction)"
                        size="small" style="margin-right: 4px">
                  AT #{{ f.at_id }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.state === 0 ? 'danger' : 'info'">
                  {{ row.state === 0 ? '进行中' : '已结束' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="开始" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.started_at) }}</template>
            </el-table-column>
            <el-table-column label="结束" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.ended_at) }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!battles.length" description="无会战记录" :image-size="60" />
        </el-tab-pane>

        <!-- 城市归属 -->
        <el-tab-pane :label="`城市归属（${cities.length}）`" name="cities">
          <el-table :data="cities" size="small" max-height="480">
            <el-table-column prop="battlefield_id" label="战场 ID" width="120" />
            <el-table-column label="归属" width="160">
              <template #default="{ row }">
                <el-tag v-if="row.owner_faction" :type="factionTag(row.owner_faction)" size="small">
                  {{ row.owner_name }}
                </el-tag>
                <el-tag v-else size="small" type="info">中立</el-tag>
              </template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!cities.length" description="无城市归属数据" :image-size="60" />
        </el-tab-pane>

        <!-- 战争史 -->
        <el-tab-pane label="战争史" name="history">
          <el-table :data="overview.history" size="small">
            <el-table-column prop="id" label="ID" width="70" />
            <el-table-column prop="name" label="名称" min-width="130" />
            <el-table-column label="状态" width="90">
              <template #default="{ row }">
                <el-tag size="small" :type="row.state === 1 ? 'success' : 'info'">
                  {{ row.state === 1 ? '进行中' : '已结束' }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column label="胜方" width="140">
              <template #default="{ row }">
                <el-tag v-if="row.winning_faction_id" :type="factionTag(row.winning_faction_id)"
                        size="small">{{ row.winning_faction }}</el-tag>
                <span v-else>-</span>
              </template>
            </el-table-column>
            <el-table-column prop="points_needed_for_victory" label="胜利点数" width="90" align="right" />
            <el-table-column label="开战" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.begintime) }}</template>
            </el-table-column>
            <el-table-column label="结束" min-width="165">
              <template #default="{ row }">{{ fmtTime(row.endtime) }}</template>
            </el-table-column>
            <el-table-column label="续战" width="90">
              <template #default="{ row }">{{ row.sequel_war_id || '-' }}</template>
            </el-table-column>
          </el-table>
          <el-empty v-if="!overview.history.length" description="无战争记录" :image-size="60" />
        </el-tab-pane>
      </el-tabs>
    </template>

    <!-- 生成 AT 对话框 -->
    <el-dialog v-model="createDlg" title="生成 AT（作弊，不扣款）" width="420px">
      <el-form label-width="90px" size="small">
        <el-form-item label="玩家 ID">
          <el-input-number v-model="createForm.owner_player_id" :min="1" controls-position="right" />
        </el-form-item>
        <el-form-item label="模板 ID">
          <el-input-number v-model="createForm.template_id" :min="1" controls-position="right" />
        </el-form-item>
        <el-form-item label="阵营">
          <el-select v-model="createForm.faction">
            <el-option label="US" :value="1" />
            <el-option label="GE" :value="2" />
            <el-option label="SU" :value="3" />
          </el-select>
        </el-form-item>
        <el-form-item label="名称">
          <el-input v-model="createForm.name" maxlength="64" placeholder="留空自动命名" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createDlg = false">取消</el-button>
        <el-button type="primary" :loading="createLoading" @click="submitCreateAt">生成</el-button>
      </template>
    </el-dialog>

    <!-- 调整 warfunds 对话框 -->
    <el-dialog v-model="fundsDlg" title="调整 Warfunds" width="420px">
      <el-form label-width="90px" size="small">
        <el-form-item label="玩家 ID">
          <el-input-number v-model="fundsForm.player_id" :min="1" controls-position="right" />
        </el-form-item>
        <el-form-item label="调整值">
          <el-input-number v-model="fundsForm.delta" controls-position="right" />
          <div class="funds-hint">正数发放、负数扣回；流水记录操作管理员</div>
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="fundsDlg = false">取消</el-button>
        <el-button type="primary" :loading="fundsLoading" @click="submitFunds">调整</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style lang="scss" scoped>
.war-head {
  margin-bottom: 12px;
}

.stat-cards {
  display: flex;
  gap: 12px;
  margin-bottom: 16px;
  flex-wrap: wrap;
}

.stat-card {
  flex: 1;
  min-width: 180px;
  border: 1px solid rgba(201, 185, 106, 0.25);
  border-radius: 6px;
  padding: 10px 14px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.stat-title {
  font-weight: 700;
}

.stat-line {
  font-size: 13px;
}

.stat-sub {
  color: #8a9360;
  font-size: 12px;
}

.toolbar {
  display: flex;
  gap: 8px;
  margin-bottom: 10px;
  align-items: center;
}

.funds-hint {
  color: #8a9360;
  font-size: 12px;
  line-height: 1.4;
}

.pager {
  margin-top: 10px;
  display: flex;
  justify-content: flex-end;
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
