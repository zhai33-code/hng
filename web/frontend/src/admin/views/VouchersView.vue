<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { get, post } from '../api'
import { fmtTime } from '../format'
import { VOUCHER_OPTIONS } from '../data/voucherOptions'

// 与后端 _ITEM_TYPES / _CURRENCIES 白名单一致；道具选项为 WarDatabase 提取的静态数据
const ITEM_TYPE_OPTIONS = [
  { value: 'credits', label: 'Credits（货币）', currency: true },
  { value: 'warfunds', label: 'Warfunds（货币）', currency: true },
  { value: 'gold', label: 'Gold（金币）', currency: true },
  { value: 'weaponvarianttemplate', label: '武器（皮肤变体）', currency: false },
  { value: 'vehiclevarianttemplate', label: '载具（涂装变体）', currency: false },
  { value: 'uniformparttemplate', label: '头盔/面罩', currency: false },
  { value: 'ribbonboostertemplate', label: '勋带加成', currency: false },
  { value: 'shopmembershipitems', label: '老兵会员', currency: false },
]

function itemLabel(type: string, id?: number): string {
  const opt = VOUCHER_OPTIONS[type]?.find(o => o.value === id)
  return opt ? opt.label : `#${id}`
}

// 切换类别后清空已选（模板 id / 数量的取值空间随类别变化，残留旧值会发错东西）
function onTypeChange(it: ItemSpec) {
  it.id = null
  it.amount = null
}

interface ItemSpec {
  type: string
  id: number | null
  amount: number | null
}

interface VoucherRow {
  id: number
  code: string
  items: { type: string; id?: number; amount?: number }[]
  note: string
  expires_at: string | null
  max_redemptions: number
  used_count: number
  last_redeemer: string
  created_at: string
}

const loading = ref(false)
const rows = ref<VoucherRow[]>([])
const total = ref(0)
const stateFilter = ref('all')
const page = reactive({ n: 1, size: 20 })

const genForm = reactive({
  count: 1,
  items: [{ type: 'credits', id: null, amount: 1000 }] as ItemSpec[],
  expiresDays: 0,
  maxRedemptions: 1,
  note: '',
})
const generating = ref(false)
const genResult = ref<string[]>([])

function addItem() {
  genForm.items.push({ type: 'gold', id: null, amount: null })
}
function removeItem(i: number) {
  genForm.items.splice(i, 1)
}
function isCurrency(t: string) {
  return ITEM_TYPE_OPTIONS.some(o => o.value === t && o.currency)
}

function itemsText(items: VoucherRow['items']) {
  return items.map(it => {
    const opt = ITEM_TYPE_OPTIONS.find(o => o.value === it.type)
    const label = opt ? opt.label : it.type
    if (it.amount !== undefined) return `${Number(it.amount).toLocaleString()} ${label}`
    return `${label}：${itemLabel(it.type, it.id)}`
  }).join(' + ')
}

function rowStatus(r: VoucherRow): { text: string; type: 'success' | 'info' | 'warning' | 'danger' } {
  if (r.expires_at && new Date(r.expires_at) <= new Date())
    return { text: '已过期', type: 'danger' }
  if (r.max_redemptions > 0 && r.used_count >= r.max_redemptions)
    return { text: '已用完', type: 'info' }
  if (r.used_count > 0) return { text: '兑换中', type: 'warning' }
  return { text: '可用', type: 'success' }
}

function quotaText(r: VoucherRow) {
  return r.max_redemptions === 0
    ? `${r.used_count} / ∞`
    : `${r.used_count} / ${r.max_redemptions}`
}

async function load() {
  loading.value = true
  try {
    const data = await get<{ total: number; items: VoucherRow[] }>(
      `/vouchers?page=${page.n}&page_size=${page.size}&state=${stateFilter.value}`)
    rows.value = data.items
    total.value = data.total
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '加载失败')
  } finally {
    loading.value = false
  }
}

async function generate() {
  const items = genForm.items
    .filter(it => (isCurrency(it.type) ? (it.amount ?? 0) > 0 : (it.id ?? 0) > 0))
    .map(it => isCurrency(it.type)
      ? { type: it.type, amount: it.amount ?? 0 }
      : { type: it.type, id: it.id ?? 0 })
  if (!items.length) {
    ElMessage.warning('请至少配置一项有效内容（货币填数量 / 道具填模板 id）')
    return
  }
  generating.value = true
  genResult.value = []
  try {
    const data = await post<{ ok: boolean; count: number; codes: string[] }>(
      '/vouchers/generate', {
      count: genForm.count,
      items,
      expires_days: genForm.expiresDays,
      max_redemptions: genForm.maxRedemptions,
      note: genForm.note,
    })
    genResult.value = data.codes
    ElMessage.success(`已生成 ${data.count} 个兑换码`)
    await load()
  } catch (e: any) {
    ElMessage.error(e?.response?.data?.detail || '生成失败')
  } finally {
    generating.value = false
  }
}

async function copyAll() {
  await navigator.clipboard.writeText(genResult.value.join('\n'))
  ElMessage.success('已复制到剪贴板')
}

async function copyCode(code: string) {
  await navigator.clipboard.writeText(code)
  ElMessage.success('已复制')
}

onMounted(load)
</script>

<template>
  <div class="page">
    <h2>兑换码</h2>

    <el-card shadow="never" class="mb">
      <template #header><b>批量生成</b></template>
      <el-form label-width="90px" style="max-width: 720px">
        <el-form-item label="数量">
          <el-input-number v-model="genForm.count" :min="1" :max="500" />
        </el-form-item>
        <el-form-item label="内容">
          <div v-for="(it, i) in genForm.items" :key="i" class="item-row">
            <el-select v-model="it.type" style="width: 200px" @change="onTypeChange(it)">
              <el-option v-for="o in ITEM_TYPE_OPTIONS" :key="o.value" :value="o.value" :label="o.label" />
            </el-select>
            <el-input-number v-if="isCurrency(it.type)" v-model="it.amount" :min="1" :max="100000000"
              placeholder="数量" style="width: 160px" />
            <el-select-v2 v-else v-model="it.id" :options="VOUCHER_OPTIONS[it.type] || []"
              filterable clearable placeholder="搜索选择道具" style="width: 340px" />
            <el-button type="danger" text :disabled="genForm.items.length <= 1" @click="removeItem(i)">
              删除
            </el-button>
          </div>
          <el-button text type="primary" @click="addItem">+ 添加内容</el-button>
        </el-form-item>
        <el-form-item label="有效期">
          <el-input-number v-model="genForm.expiresDays" :min="0" :max="3650" />
          <span class="hint">天（0 = 永久）</span>
        </el-form-item>
        <el-form-item label="总限量">
          <el-input-number v-model="genForm.maxRedemptions" :min="0" :max="100000" />
          <span class="hint">次（1 = 一码一用；0 = 无限次，每人限一次）</span>
        </el-form-item>
        <el-form-item label="备注">
          <el-input v-model="genForm.note" maxlength="128" placeholder="批次用途，如：国庆活动" style="width: 320px" />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" :loading="generating" @click="generate">生成</el-button>
        </el-form-item>
      </el-form>
      <div v-if="genResult.length" class="gen-result">
        <el-alert type="success" :closable="false">
          <template #title>
            已生成 {{ genResult.length }} 个码
            <el-button text type="primary" size="small" @click="copyAll">全部复制</el-button>
          </template>
        </el-alert>
      </div>
    </el-card>

    <el-card shadow="never">
      <div class="toolbar">
        <el-select v-model="stateFilter" style="width: 140px" @change="page.n = 1; load()">
          <el-option value="all" label="全部" />
          <el-option value="unused" label="可用" />
          <el-option value="used" label="已兑换" />
          <el-option value="exhausted" label="已用完" />
          <el-option value="expired" label="已过期" />
        </el-select>
        <el-button @click="load">刷新</el-button>
      </div>
      <el-table v-loading="loading" :data="rows" size="small">
        <el-table-column label="兑换码" width="150">
          <template #default="{ row }">
            <el-link type="primary" @click="copyCode(row.code)">{{ row.code }}</el-link>
          </template>
        </el-table-column>
        <el-table-column label="内容" min-width="240">
          <template #default="{ row }">{{ itemsText(row.items) }}</template>
        </el-table-column>
        <el-table-column label="备注" prop="note" width="120" show-overflow-tooltip />
        <el-table-column label="已兑/限量" width="90">
          <template #default="{ row }">{{ quotaText(row) }}</template>
        </el-table-column>
        <el-table-column label="最近兑换人" prop="last_redeemer" width="110" />
        <el-table-column label="过期时间" width="170">
          <template #default="{ row }">{{ row.expires_at ? fmtTime(row.expires_at) : '永久' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="90">
          <template #default="{ row }">
            <el-tag :type="rowStatus(row).type" size="small">{{ rowStatus(row).text }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="创建时间" width="170">
          <template #default="{ row }">{{ fmtTime(row.created_at) }}</template>
        </el-table-column>
      </el-table>
      <el-pagination class="pager" layout="total, prev, pager, next" :total="total"
        :page-size="page.size" v-model:current-page="page.n" @current-change="load" />
    </el-card>
  </div>
</template>

<style scoped>
.item-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}
.hint {
  margin-left: 8px;
  color: var(--el-text-color-secondary);
  font-size: 12px;
}
.gen-result {
  margin-top: 12px;
}
</style>
