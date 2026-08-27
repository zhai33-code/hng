<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get } from '../api'

interface WeaponItem {
  id: number
  name: string
  template_id: number
  template_name: string
  variant_template_id: number
  equipped: boolean
  container_id: number
  container_slot: number
  modifiers: { id: number; template_id: number; name: string }[]
  ammo: { id: number; template_id: number; name: string; slot_index: number; condition: number }[]
}

interface VehicleItem {
  id: number
  variant_template_id: number
  name: string
  parent: number
  parent_name: string
  equipped_in_slot: number
  upgrades: { id: number; template_id: number; equipped_slot_id: number }[]
}

interface CharacterDetail {
  character: {
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
  weapons: WeaponItem[]
  vehicles: VehicleItem[]
  ribbons: { ribbon_template_id: number; name: string; total_xp: number }[]
}

const route = useRoute()
const playerId = computed(() => Number(route.params.id))
const characterId = computed(() => Number(route.params.cid))
const loading = ref(true)
const data = ref<CharacterDetail | null>(null)

async function load() {
  loading.value = true
  try {
    data.value = await get<CharacterDetail>(
      `/players/${playerId.value}/characters/${characterId.value}`)
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

/** 载具图标：客户端 images/Vehicles/{vehicleTemplate id}.png（variant 级无图，用父模板图） */
function vehicleIcon(v: VehicleItem): string {
  return `/icons/Vehicles/${v.parent}.png`
}
/** 勋带图标：客户端 images/Ribbons/{ribbonTemplate id}.png */
function ribbonIcon(r: { ribbon_template_id: number }): string {
  return `/icons/Ribbons/${r.ribbon_template_id}.png`
}
/** 军衔徽章 */
function rankIcon(): string {
  const c = data.value?.character
  return c ? `/icons/Rank/${c.faction}_1_${c.rank}.png` : ''
}
function onImgError(e: Event) {
  ;(e.target as HTMLElement).style.display = 'none'
}

const equippedWeapons = computed(() => data.value?.weapons.filter((w) => w.equipped) ?? [])
const storedWeapons = computed(() => data.value?.weapons.filter((w) => !w.equipped) ?? [])
</script>

<template>
  <div v-loading="loading">
    <template v-if="data">
      <!-- 角色概要 -->
      <div class="panel char-head">
        <img class="rank-badge" :src="rankIcon()" :alt="`军衔 ${data.character.rank}`"
             @error="onImgError" />
        <div class="char-info">
          <div class="char-name">{{ data.character.name }}</div>
          <div class="char-meta">
            {{ data.character.class_name }} · {{ data.character.faction_name }} ·
            军衔 {{ data.character.rank }} · 战斗经验 {{ data.character.combat_xp }}
            <template v-if="data.character.badge_0 || data.character.badge_1">
              · 徽章 {{ [data.character.badge_0, data.character.badge_1].filter(Boolean).join(' / ') }}
            </template>
            · 创建于 {{ fmtTime(data.character.created_at) }}
          </div>
        </div>
        <el-button text type="primary" @click="$router.back()">返回玩家</el-button>
      </div>

      <!-- 武器（已装备） -->
      <div class="gear-section">
        <span class="section-title">已装备武器（{{ equippedWeapons.length }}）</span>
        <div v-if="equippedWeapons.length" class="gear-list">
          <div v-for="w in equippedWeapons" :key="w.id" class="gear-card">
            <div class="gear-main">
              <div class="gear-name">{{ w.template_name }}</div>
              <div class="gear-meta">
                自定义名 "{{ w.name }}" · 槽位 {{ w.container_slot }} · 变体 #{{ w.variant_template_id }}
              </div>
              <div v-if="w.modifiers.length || w.ammo.length" class="gear-parts">
                <span v-for="m in w.modifiers" :key="'m' + m.id" class="part-tag">
                  配件 · {{ m.name }}
                </span>
                <span v-for="a in w.ammo" :key="'a' + a.id" class="part-tag">
                  弹药 · {{ a.name }}（{{ (a.condition * 100).toFixed(0) }}%）
                </span>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-else description="无已装备武器" :image-size="50" />
      </div>

      <!-- 武器（仓库） -->
      <div v-if="storedWeapons.length" class="gear-section">
        <span class="section-title">仓库武器（{{ storedWeapons.length }}）</span>
        <div class="gear-list">
          <div v-for="w in storedWeapons" :key="w.id" class="gear-card">
            <div class="gear-main">
              <div class="gear-name">{{ w.template_name }}</div>
              <div class="gear-meta">自定义名 "{{ w.name }}" · 变体 #{{ w.variant_template_id }}</div>
              <div v-if="w.modifiers.length || w.ammo.length" class="gear-parts">
                <span v-for="m in w.modifiers" :key="'m' + m.id" class="part-tag">
                  配件 · {{ m.name }}
                </span>
                <span v-for="a in w.ammo" :key="'a' + a.id" class="part-tag">
                  弹药 · {{ a.name }}（{{ (a.condition * 100).toFixed(0) }}%）
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 载具 -->
      <div class="gear-section">
        <span class="section-title">载具（{{ data.vehicles.length }}）</span>
        <div v-if="data.vehicles.length" class="gear-list">
          <div v-for="v in data.vehicles" class="gear-card">
            <img class="gear-icon" :src="vehicleIcon(v)" :alt="v.name" @error="onImgError" />
            <div class="gear-main">
              <div class="gear-name">{{ v.name }}</div>
              <div class="gear-meta">
                {{ v.parent_name }} · 装备槽 {{ v.equipped_in_slot }} · 变体 #{{ v.variant_template_id }}
              </div>
              <div v-if="v.upgrades.length" class="gear-parts">
                <span v-for="u in v.upgrades" :key="u.id" class="part-tag">
                  升级件 #{{ u.template_id }}（槽 {{ u.equipped_slot_id }}）
                </span>
              </div>
            </div>
          </div>
        </div>
        <el-empty v-else description="无载具" :image-size="50" />
      </div>

      <!-- 勋带 -->
      <div class="gear-section">
        <span class="section-title">勋带（{{ data.ribbons.length }}）</span>
        <div v-if="data.ribbons.length" class="ribbon-grid">
          <div v-for="r in data.ribbons" :key="r.ribbon_template_id" class="ribbon-item">
            <img class="gear-icon" :src="ribbonIcon(r)" :alt="r.name" @error="onImgError" />
            <div class="ribbon-meta">
              <div>{{ r.name }}</div>
              <div class="ribbon-xp">{{ r.total_xp }} XP</div>
            </div>
          </div>
        </div>
        <el-empty v-else description="无勋带" :image-size="50" />
      </div>
    </template>
  </div>
</template>

<style lang="scss" scoped>
.char-head {
  display: flex;
  align-items: center;
  gap: 16px;
  margin-bottom: 20px;

  .char-info {
    flex: 1;
    min-width: 0;
  }

  .char-name {
    font-size: 18px;
    font-weight: 800;
    color: #e8d48b;
  }

  .char-meta {
    margin-top: 4px;
    font-size: 13px;
    color: #9a947e;
  }
}

.ribbon-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}

.ribbon-item {
  // 复制全局 .panel 样式（scoped 块无法 @extend 全局类）
  background: #23261b;
  border: 1px solid #3a3e2e;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;

  .ribbon-meta {
    font-size: 13px;

    .ribbon-xp {
      color: #7a7561;
      font-size: 12px;
      margin-top: 2px;
    }
  }
}
</style>
