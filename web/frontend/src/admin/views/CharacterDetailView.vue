<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage } from 'element-plus'
import { get } from '../api'
import { fmtTime } from '../format'

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
    badge_0_id: number
    badge_1_id: number
    created_at: string
  }
  weapons: WeaponItem[]
  vehicles: VehicleItem[]
  ribbons: {
    ribbon_template_id: number
    name: string
    icon: string
    overlay_icon: string
    overlay_fallback: string
    overlay_style: { scale: number; x: number; y: number }
    total_xp: number
  }[]
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

/** 武器图标：客户端 images/Weapons/{weaponTemplate id}.png */
function weaponIcon(w: WeaponItem): string {
  return `/icons/Weapons/${w.template_id}.webp`
}
/** 载具图标：客户端 images/Vehicles/{vehicleTemplate id}.png（variant 级无图，用父模板图） */
function vehicleIcon(v: VehicleItem): string {
  return `/icons/Vehicles/${v.parent}.webp`
}
/** 勋带图标：后端拼好最终路径（专属图或类别底图），前端零试错 */
/** 军衔徽章 */
function rankIcon(): string {
  const c = data.value?.character
  return c ? `/icons/Rank/${c.faction}_1_${c.rank}.png` : ''
}
function onImgError(e: Event) {
  ;(e.target as HTMLElement).style.display = 'none'
}

/** 叠加图加载失败：先降级到武器/载具全尺寸图，再失败则隐藏 */
function onOverlayError(e: Event, r: { overlay_fallback: string }) {
  const img = e.target as HTMLImageElement
  if (img.dataset.fb !== '1' && r.overlay_fallback) {
    img.dataset.fb = '1'
    img.src = r.overlay_fallback
    img.classList.add('fallback')
  } else {
    img.style.display = 'none'
  }
}

/** pip 摆放（官方 PipTransform）：高度 = scale×底图高，中心定位 (x, y)；
 * 后端字段缺失时回退官方 smg 默认值，保证居中 */
function pipStyle(r: { overlay_style?: { scale: number; x: number; y: number } }) {
  const s = r.overlay_style ?? { scale: 0.65, x: 0.5, y: 0.5 }
  return {
    height: `${s.scale * 100}%`,
    left: `${s.x * 100}%`,
    top: `${s.y * 100}%`,
  }
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
        <!-- 战斗徽章：图标在上、文字在下 -->
        <div v-if="data.character.badge_0 || data.character.badge_1" class="badges">
          <div v-for="b in [{ id: data.character.badge_0_id, name: data.character.badge_0 },
                            { id: data.character.badge_1_id, name: data.character.badge_1 }].filter(b => b.name)"
               :key="b.id" class="badge-item">
            <img class="combat-badge"
                 :src="b.id ? `/icons/CombatBadges/${b.id}.png` : ''"
                 :alt="b.name" :title="b.name" @error="onImgError" />
            <span class="badge-name">{{ b.name }}</span>
          </div>
        </div>
        <div class="char-info">
          <div class="char-name">{{ data.character.name }}</div>
          <div class="char-meta">
            {{ data.character.class_name }} · {{ data.character.faction_name }} ·
            军衔 {{ data.character.rank }} · 战斗经验 {{ data.character.combat_xp }}
            · 创建于 {{ fmtTime(data.character.created_at) }}
          </div>
        </div>
        <el-button text type="primary" @click="$router.back()">返回玩家</el-button>
      </div>

      <!-- 武器（已装备） -->
      <div class="gear-section">
        <span class="section-title">已装备武器（{{ equippedWeapons.length }}）</span>
        <div v-if="equippedWeapons.length" class="gear-list weapons">
          <div v-for="w in equippedWeapons" :key="w.id" class="gear-card weapon-card">
            <div class="weapon-visual">
              <img :src="weaponIcon(w)" :alt="w.template_name" @error="onImgError" />
            </div>
            <div class="gear-main">
              <div class="gear-name">{{ w.template_name }}</div>
              <div class="gear-meta">
                "{{ w.name }}" · 槽位 {{ w.container_slot }}
              </div>
              <div v-if="w.modifiers.length || w.ammo.length" class="gear-parts">
                <span v-for="m in w.modifiers" :key="'m' + m.id" class="part-tag">
                  {{ m.name }}
                </span>
                <span v-for="a in w.ammo" :key="'a' + a.id" class="part-tag">
                  {{ a.name }}
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
        <div class="gear-list weapons">
          <div v-for="w in storedWeapons" :key="w.id" class="gear-card weapon-card">
            <div class="weapon-visual">
              <img :src="weaponIcon(w)" :alt="w.template_name" @error="onImgError" />
            </div>
            <div class="gear-main">
              <div class="gear-name">{{ w.template_name }}</div>
              <div class="gear-meta">"{{ w.name }}"</div>
              <div v-if="w.modifiers.length || w.ammo.length" class="gear-parts">
                <span v-for="m in w.modifiers" :key="'m' + m.id" class="part-tag">
                  {{ m.name }}
                </span>
                <span v-for="a in w.ammo" :key="'a' + a.id" class="part-tag">
                  {{ a.name }}
                </span>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 载具 -->
      <div class="gear-section">
        <span class="section-title">载具（{{ data.vehicles.length }}）</span>
        <div v-if="data.vehicles.length" class="gear-list vehicles">
          <div v-for="v in data.vehicles" :key="v.id" class="gear-card">
            <img class="gear-icon vehicle" :src="vehicleIcon(v)" :alt="v.name" @error="onImgError" />
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
            <div class="ribbon-container">
              <img class="gear-icon ribbon base" :src="r.icon" :alt="r.name" @error="onImgError" />
              <img v-if="r.overlay_icon || r.overlay_fallback" class="ribbon overlay"
                   :src="r.overlay_icon || r.overlay_fallback"
                   :class="{ fallback: !r.overlay_icon }" :style="pipStyle(r)"
                   :alt="r.name" @error="onOverlayError($event, r)" />
            </div>
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
// 武器卡：竖向布局（图上字下，像游戏背包格子）
// 客户端素材：Weapons 640×360 / Vehicles 320×138 / Ribbons 256×128
.gear-card.weapon-card {
  flex-direction: column;
  gap: 0;
  padding: 0;

  .weapon-visual {
    // 16:9 顶部图区，深底 + 底边线，与文字区分隔
    background: #14170e;
    border-bottom: 1px solid #33362a;
    border-radius: 5px 5px 0 0;
    overflow: hidden;
    aspect-ratio: 640 / 360;

    img {
      width: 100%;
      height: 100%;
      object-fit: contain;
      display: block;
    }
  }

  .gear-main {
    padding: 10px 12px 12px;
  }
}

.gear-icon.vehicle {
  width: 170px;
  height: 73px;
  flex-shrink: 0;
}

// 勋带图标容器：类别底图 + 官方 pip 叠加图
// 官方 updatePipImage()：pip 等比缩放到底图高度 × PipTransform.scale，
// 中心定位 (pos.x×W, pos.y×H)，配合内联样式 height/left/top + translate 复刻
.ribbon-container {
  position: relative;
  width: 132px;
  height: 66px;
  flex-shrink: 0;

  .gear-icon.ribbon {
    width: 100%;
    height: 100%;
    display: block;
    object-fit: contain;
  }

  .ribbon.overlay {
    position: absolute;
    width: auto; // 高度由内联 style 按官方 scale 给出，宽度按原图比例
    transform: translate(-50%, -50%);
    object-fit: contain;
    // 官方 pip 为灰度图并用 ColorMatrixFilter 染铜色，这里轻微提亮染色近似
    filter: brightness(1.8) sepia(0.35) saturate(2);
  }

  // 回退：武器(640x360)/载具(320x138)全尺寸图，同样缩放叠加（不染色）
  .ribbon.overlay.fallback {
    filter: none;
  }
}

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
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 10px;
}

.ribbon-item {
  // 复制全局 .panel 样式（scoped 块无法 @extend 全局类）
  background: #23261b;
  border: 1px solid #3a3e2e;
  border-radius: 6px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px 14px;

  .ribbon-meta {
    font-size: 13px;
    min-width: 0;
    overflow-wrap: anywhere;

    .ribbon-xp {
      color: #7a7561;
      font-size: 12px;
      margin-top: 2px;
    }
  }
}
</style>
