<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, put } from '../../api'
import { fmtTime } from '../../format'

interface WeatherCfg {
  tod_weights: number[]
  cond_weights: number[]
  excluded: number[][]
  snow_chance: number
}

interface WeatherResp {
  configured: boolean
  config: WeatherCfg
  updated_at: string | null
  updated_by: string
}

// 与后端 config_svc.WEATHER_DEFAULTS / 游戏服 mm_weather.py 保持一致
const DEFAULTS = {
  tod: [20, 20, 40, 20],
  cond: [50, 40, 10],
  excluded: [[0, 2], [1, 2], [3, 2]],
  snow: 30,
}

const TOD_LABELS = ['夜晚', '黎明', '白天', '黄昏']
const COND_LABELS = ['少云', '阴天', '雾']

const loading = ref(true)
const saving = ref(false)
const configured = ref(false)
const updatedAt = ref<string | null>(null)
const updatedBy = ref('')

const todPct = reactive<number[]>([...DEFAULTS.tod])
const condPct = reactive<number[]>([...DEFAULTS.cond])
const snowPct = ref(DEFAULTS.snow)
// 排除矩阵 [时段][天气]：true = 该组合不出现
const excluded = reactive(
  Array.from({ length: 4 }, () => [false, false, false]))

/** 权重 → 整数百分比（最大余数法凑整，和恒为 100） */
function toPercent(weights: number[]): number[] {
  const sum = weights.reduce((a, b) => a + b, 0)
  if (sum <= 0) return weights.map(() => 0)
  const raw = weights.map(w => (w / sum) * 100)
  const out = raw.map(v => Math.floor(v))
  const left = 100 - out.reduce((a, b) => a + b, 0)
  raw.map((v, i) => ({ i, frac: v - Math.floor(v) }))
    .sort((a, b) => b.frac - a.frac)
    .slice(0, Math.max(0, left))
    .forEach(({ i }) => { out[i] += 1 })
  return out
}

/** 100% 分配器：拖动某项后，组内其余项按比例瓜分剩余（其余全 0 时平分） */
function rebalance(group: number[], changed: number) {
  const rest = 100 - group[changed]
  const othersSum = group.reduce((a, b, i) => (i === changed ? a : a + b), 0)
  for (let i = 0; i < group.length; i++) {
    if (i === changed) continue
    if (rest <= 0) group[i] = 0
    else if (othersSum <= 0) group[i] = rest / (group.length - 1)
    else group[i] = (group[i] / othersSum) * rest
  }
}

function applyConfig(cfg: WeatherCfg) {
  todPct.splice(0, 4, ...toPercent(cfg.tod_weights))
  condPct.splice(0, 3, ...toPercent(cfg.cond_weights))
  snowPct.value = Math.round(cfg.snow_chance * 100)
  for (const row of excluded) row.fill(false)
  for (const [t, c] of cfg.excluded) {
    if (t >= 0 && t < 4 && c >= 0 && c < 3) excluded[t][c] = true
  }
}

function buildExcluded(): number[][] {
  const out: number[][] = []
  for (let t = 0; t < 4; t++)
    for (let c = 0; c < 3; c++)
      if (excluded[t][c]) out.push([t, c])
  return out
}

function resetDefaults() {
  todPct.splice(0, 4, ...DEFAULTS.tod)
  condPct.splice(0, 3, ...DEFAULTS.cond)
  snowPct.value = DEFAULTS.snow
  for (const row of excluded) row.fill(false)
  for (const [t, c] of DEFAULTS.excluded) excluded[t][c] = true
  ElMessage.info('已恢复默认值（尚未保存）')
}

async function load() {
  loading.value = true
  try {
    const data = await get<WeatherResp>('/config/weather')
    configured.value = data.configured
    updatedAt.value = data.updated_at
    updatedBy.value = data.updated_by
    applyConfig(data.config)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function save() {
  try {
    await ElMessageBox.confirm(
      '保存后下一局成局生效，成局中的战局不受影响。', '保存配置',
      { type: 'warning' })
  } catch {
    return
  }
  saving.value = true
  try {
    await put('/config/weather', {
      tod_weights: toPercent(todPct),
      cond_weights: toPercent(condPct),
      excluded: buildExcluded(),
      snow_chance: snowPct.value / 100,
    })
    ElMessage.success('已保存，下一局成局生效')
    await load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="weather-panel">
    <div class="panel">
      <div class="head">
        <div class="head-left">
          <span class="head-title">战局天气</span>
          <el-tag :type="configured ? 'success' : 'info'" size="small">
            {{ configured ? '已保存 · 对后续成局生效' : '未保存过 · 当前为代码默认' }}
          </el-tag>
        </div>
        <span v-if="configured" class="head-meta">
          上次保存 {{ fmtTime(updatedAt) }} · {{ updatedBy || '-' }}
        </span>
      </div>

      <el-alert class="alert" type="info" :closable="false" show-icon>
        游戏服当前使用{{ configured ? '已保存的配置' : '代码内置默认（下雪概率受环境变量 MM_SNOW_CHANCE 影响）' }}。
        保存后以本页配置为准，对后续新成局立即生效，无需重启服务器；拖动滑条时同组其他项自动按比例调整。
      </el-alert>

      <div class="cfg-group">
        <div class="group-title">时段分布</div>
        <div class="group-sub">
          每局先抽时段，光照/太阳角度由时段自带（夜 0 点 · 黎明 6 点 · 白天 12 点 · 黄昏 18 点）
        </div>
        <div v-for="(label, i) in TOD_LABELS" :key="label" class="slider-row">
          <span class="slider-label">{{ label }}</span>
          <el-slider v-model="todPct[i]" :min="0" :max="100" :step="1"
                     @input="rebalance(todPct, i)" />
          <span class="slider-value">{{ Math.round(todPct[i]) }}%</span>
        </div>
      </div>

      <div class="cfg-group">
        <div class="group-title">天气分布</div>
        <div class="group-sub">与时段独立抽取，实际组合概率 = 两者相乘</div>
        <div v-for="(label, i) in COND_LABELS" :key="label" class="slider-row">
          <span class="slider-label">{{ label }}</span>
          <el-slider v-model="condPct[i]" :min="0" :max="100" :step="1"
                     @input="rebalance(condPct, i)" />
          <span class="slider-value">{{ Math.round(condPct[i]) }}%</span>
        </div>
      </div>

      <div class="cfg-group">
        <div class="group-title">下雪概率</div>
        <div class="group-sub">雪效需要客户端与服务端均已替换补丁版 player.dll</div>
        <div class="slider-row">
          <span class="slider-label">下雪</span>
          <el-slider v-model="snowPct" :min="0" :max="100" :step="1" />
          <span class="slider-value">{{ snowPct }}%</span>
        </div>
      </div>

      <el-collapse class="adv">
        <el-collapse-item name="excluded">
          <template #title>
            <span class="adv-title">高级：排除特定组合</span>
          </template>
          <div class="group-sub adv-hint">
            勾选 = 该组合不出现；若某时段三种天气全部勾选，该时段将不会出现
          </div>
          <div v-for="(label, t) in TOD_LABELS" :key="label" class="excl-row">
            <span class="slider-label">{{ label }}</span>
            <el-checkbox v-for="(cl, c) in COND_LABELS" :key="cl"
                         v-model="excluded[t][c]">{{ cl }}</el-checkbox>
          </div>
        </el-collapse-item>
      </el-collapse>

      <div class="btn-bar">
        <el-button plain @click="resetDefaults">恢复默认值</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.weather-panel {
  max-width: 780px;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.head-title {
  font-weight: 700;
  font-size: 15px;
}

.head-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.alert {
  margin-bottom: 22px;
}

.cfg-group {
  margin-bottom: 24px;
}

.group-title {
  font-weight: 700;
  margin-bottom: 4px;
}

.group-sub {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  margin-bottom: 10px;
}

.slider-row {
  display: flex;
  align-items: center;
  gap: 12px;

  :deep(.el-slider) {
    flex: 1;
  }
}

.slider-label {
  width: 56px;
  flex-shrink: 0;
  font-size: 13px;
}

.slider-value {
  width: 44px;
  flex-shrink: 0;
  text-align: right;
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}

.adv {
  margin-bottom: 4px;
}

.adv-title {
  font-size: 13px;
}

.adv-hint {
  margin-bottom: 12px;
}

.excl-row {
  display: flex;
  align-items: center;
  gap: 20px;
  margin-bottom: 4px;
}

.btn-bar {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 20px;
}
</style>
