<template>
  <el-form
    ref="formRef"
    :model="form"
    :rules="rules"
    label-position="top"
    size="large"
    @submit.prevent
  >
    <!-- 区块一：外服账号 -->
    <div class="section-title">外服账号（数据来源）</div>
    <el-form-item prop="foreign_username" label="外服用户名">
      <el-input
        v-model="form.foreign_username"
        placeholder="官方服务器的账号"
        maxlength="64"
        autocomplete="username"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="foreign_password" label="外服密码">
      <el-input
        v-model="form.foreign_password"
        type="password"
        placeholder="官方服务器的密码"
        show-password
        autocomplete="current-password"
        :disabled="loading"
      />
    </el-form-item>

    <!-- 区块二：本服新账号 -->
    <div class="section-title">本服新账号（迁移目标）</div>
    <el-form-item prop="username" label="新用户名">
      <el-input
        v-model="form.username"
        placeholder="2-24 位字母 / 数字 / _ - . 组合"
        maxlength="24"
        autocomplete="username"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="password" label="密码">
      <el-input
        v-model="form.password"
        type="password"
        placeholder="6-64 位"
        show-password
        autocomplete="new-password"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="password2" label="确认密码">
      <el-input
        v-model="form.password2"
        type="password"
        placeholder="再次输入密码"
        show-password
        autocomplete="new-password"
        :disabled="loading"
      />
    </el-form-item>

    <!-- 蜜罐：正常用户不可见，机器人填写即拒绝 -->
    <div aria-hidden="true">
      <input
        v-model="form.website"
        class="hp-field"
        name="website"
        tabindex="-1"
        autocomplete="off"
      />
      <input
        v-model="form.nickname2"
        class="hp-field"
        name="nickname2"
        tabindex="-1"
        autocomplete="off"
      />
    </div>

    <el-button
      class="hng-btn submit-btn"
      type="primary"
      native-type="submit"
      :loading="loading"
      @click="submit"
    >
      {{ loading ? loadingText : '开始迁移' }}
    </el-button>

    <transition name="el-fade-in">
      <el-alert
        v-if="result"
        :title="result.message"
        :type="result.type"
        :closable="false"
        show-icon
        class="result-alert"
      />
    </transition>
  </el-form>

  <!-- 迁移成功结果卡 -->
  <transition name="el-fade-in">
    <div v-if="summary" class="summary-card">
      <div class="summary-head">
        <span class="tag">迁移完成</span>
        <span class="gamertag">{{ summary.foreign_gamertag }}</span>
        <span class="arrow">→</span>
        <span class="local-user">{{ summary.username }}</span>
      </div>
      <div class="summary-currencies">
        <div class="cur">
          <span class="cur-label">黄金</span>
          <span class="cur-val">{{ fmt(summary.currencies?.gold) }}</span>
        </div>
        <div class="cur">
          <span class="cur-label">信贷</span>
          <span class="cur-val">{{ fmt(summary.currencies?.credits) }}</span>
        </div>
        <div class="cur">
          <span class="cur-label">战争资金</span>
          <span class="cur-val">{{ fmt(summary.currencies?.warfunds) }}</span>
        </div>
      </div>
      <div class="summary-counts">
        <div v-for="row in countRows" :key="row.key" class="count-row">
          <span class="count-label">{{ row.label }}</span>
          <span class="count-val">{{ row.value }}</span>
        </div>
      </div>
      <p class="summary-tip">
        现在可以启动游戏客户端，使用新账号 <b>{{ summary.username }}</b> 登录本服。
      </p>
    </div>
  </transition>

  <el-dialog
    v-model="captchaVisible"
    title="安全验证"
    width="352"
    align-center
    :close-on-click-modal="false"
    @open="loadCaptcha"
  >
    <div v-loading="capLoading" class="captcha-box">
      <Slide
        v-if="slideData.image"
        ref="capRef"
        :data="slideData"
        :events="capEvents"
      />
    </div>
  </el-dialog>
</template>

<script setup lang="ts">
import { computed, reactive, ref } from 'vue'
import {
  ElForm, ElInput, ElButton, ElAlert, ElDialog, ElMessage,
  FormRules, FormInstance,
} from 'element-plus'
import { Slide } from 'go-captcha-vue'
import 'go-captcha-vue/dist/style.css'
import { apiGet, apiPost, ApiError } from '../register/api'

interface SlideData {
  thumbX: number
  thumbY: number
  thumbWidth: number
  thumbHeight: number
  image: string
  thumb: string
}

interface CaptchaChallenge extends SlideData {
  captcha_id: string
}

interface ImportSummary {
  player_id: number
  username: string
  foreign_gamertag: string
  currencies: { gold?: number; credits?: number; warfunds?: number }
  counts: Record<string, number>
}

// 导入全流程（外服登录 + 15s 采集 + 落库）服务端上限 120s，前端超时放宽到 150s
const IMPORT_TIMEOUT_MS = 150000

const COUNT_LABELS: Record<string, string> = {
  characters: '士兵',
  weapons: '武器',
  vehicles: '载具',
  weapon_variants: '武器改装',
  weapon_modifiers: '武器配件',
  weapon_ammo: '武器弹药',
  vehicle_variants: '载具改装',
  vehicle_upgrades: '载具升级',
  vehicle_ammo: '载具弹药',
  uniform_variants: '军装外观',
  uniform_parts: '军装部件',
  ribbon_xp_rows: '勋带经验',
  stat_rows: '战斗统计',
  tracer_effects: '曳光弹效果',
  skipped_refs: '跳过的失效引用',
}

const formRef = ref<FormInstance>()
const loading = ref(false)
const loadingText = ref('正在从外服采集数据…')
const result = ref<{ type: 'success' | 'error'; message: string } | null>(null)
const summary = ref<ImportSummary | null>(null)

const form = reactive({
  foreign_username: '',
  foreign_password: '',
  username: '',
  password: '',
  password2: '',
  website: '',    // 蜜罐
  nickname2: '',  // 蜜罐
})

const rules: FormRules = {
  foreign_username: [
    { required: true, message: '请输入外服用户名', trigger: 'blur' },
  ],
  foreign_password: [
    { required: true, message: '请输入外服密码', trigger: 'blur' },
  ],
  username: [
    { required: true, message: '请输入新用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_\-.]{2,24}$/, message: '2-24 位字母/数字/_-. 组合', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 6, max: 64, message: '密码需为 6-64 位', trigger: 'blur' },
  ],
  password2: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (v === form.password ? cb() : cb(new Error('两次输入的密码不一致'))),
      trigger: 'blur',
    },
  ],
}

const countRows = computed(() => {
  const counts = summary.value?.counts || {}
  return Object.entries(COUNT_LABELS)
    .filter(([key]) => counts[key] !== undefined)
    .map(([key, label]) => ({ key, label, value: counts[key] }))
})

function fmt(v?: number) {
  return (v ?? 0).toLocaleString('zh-CN')
}

// ---- 验证码（与注册页同一套后端接口） ----

const captchaVisible = ref(false)
const capLoading = ref(false)
const capChecking = ref(false)
const capRef = ref<{ reset: () => void; clear: () => void }>()
const captchaId = ref('')
const slideData = reactive<SlideData>({
  thumbX: 0, thumbY: 0, thumbWidth: 0, thumbHeight: 0, image: '', thumb: '',
})

async function loadCaptcha() {
  capLoading.value = true
  slideData.image = ''
  try {
    const data = await apiGet<CaptchaChallenge>('/api/captcha/get')
    captchaId.value = data.captcha_id
    slideData.thumbX = data.thumbX
    slideData.thumbY = data.thumbY
    slideData.thumbWidth = data.thumbWidth
    slideData.thumbHeight = data.thumbHeight
    slideData.image = data.image
    slideData.thumb = data.thumb
  } catch (e) {
    const err = e as ApiError
    ElMessage.error(err.message || '验证码加载失败，请稍后再试')
    captchaVisible.value = false
  } finally {
    capLoading.value = false
  }
}

const capEvents = {
  refresh: () => loadCaptcha(),
  confirm: (point: { x: number; y: number }, reset: () => void) => {
    void onCaptchaConfirm(point, reset)
  },
}

async function onCaptchaConfirm(point: { x: number; y: number }, reset: () => void) {
  if (capChecking.value || !captchaId.value) return
  capChecking.value = true
  try {
    const r = await apiPost<{ ok: boolean; token: string }>('/api/captcha/check', {
      captcha_id: captchaId.value,
      x: point.x,
      y: point.y,
    })
    captchaVisible.value = false
    await doImport(r.token)
  } catch (e) {
    const err = e as ApiError
    ElMessage.error(err.message || '验证失败，请重试')
    reset()
    loadCaptcha()
  } finally {
    capChecking.value = false
  }
}

async function submit() {
  if (loading.value) return
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  captchaVisible.value = true   // @open 触发 loadCaptcha
}

// 采集阶段进度文案轮换：过程不透明（单次长请求），给用户阶段感
const PROGRESS_STEPS = [
  '正在登录外服服务器…',
  '正在采集士兵与装备数据…',
  '正在写入本服数据库…',
]
let progressTimer: ReturnType<typeof setInterval> | undefined

async function doImport(token: string) {
  loading.value = true
  result.value = null
  summary.value = null
  let step = 0
  loadingText.value = PROGRESS_STEPS[0]
  clearInterval(progressTimer)
  progressTimer = setInterval(() => {
    step = Math.min(step + 1, PROGRESS_STEPS.length - 1)
    loadingText.value = PROGRESS_STEPS[step]
  }, 12000)
  try {
    const data = await apiPost<ImportSummary>('/api/import', {
      foreign_username: form.foreign_username,
      foreign_password: form.foreign_password,
      username: form.username,
      password: form.password,
      token,
      website: form.website,
      nickname2: form.nickname2,
    }, IMPORT_TIMEOUT_MS)
    summary.value = data
    result.value = {
      type: 'success',
      message: `迁移成功！外服账号 ${data.foreign_gamertag} 的数据已导入本服。`,
    }
    form.foreign_username = form.foreign_password = ''
    form.username = form.password = form.password2 = ''
  } catch (e) {
    const err = e as ApiError
    result.value = { type: 'error', message: err.message || '迁移失败，请稍后重试' }
  } finally {
    clearInterval(progressTimer)
    loading.value = false
  }
}
</script>

<style scoped lang="scss">
@use '@/styles/vars' as *;

:deep(.el-form-item__label) {
  color: $hng-khaki;
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-weight: 700;
  padding-bottom: 4px;
}

.section-title {
  color: $hng-gold;
  font-size: 12px;
  font-weight: 800;
  letter-spacing: 0.22em;
  text-transform: uppercase;
  margin: 6px 0 12px;
  padding-bottom: 6px;
  border-bottom: 1px dashed rgba(200, 185, 138, 0.25);
  display: flex;
  align-items: center;
  gap: 8px;

  &::before {
    content: '';
    width: 8px;
    height: 8px;
    background: $hng-gold;
    clip-path: polygon(50% 0, 100% 50%, 50% 100%, 0 50%);
  }
}

.submit-btn {
  width: 100%;
  margin-top: 8px;
  height: 46px;
  font-size: 16px;
  letter-spacing: 0.2em;
}

.result-alert {
  margin-top: 18px;
}

// 蜜罐字段：移出视口
.hp-field {
  position: absolute;
  left: -9999px;
  top: -9999px;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
}

.captcha-box {
  display: flex;
  justify-content: center;
  min-height: 260px;
}

// ---- 迁移结果卡 ----
.summary-card {
  margin-top: 20px;
  border: 1px solid rgba(200, 185, 138, 0.3);
  background: linear-gradient(160deg, rgba(28, 32, 18, 0.95), rgba(16, 18, 10, 0.95));
  padding: 16px 18px;
}

.summary-head {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding-bottom: 12px;
  border-bottom: 1px solid rgba(200, 185, 138, 0.18);

  .tag {
    background: $hng-olive;
    color: #0e100a;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.14em;
    padding: 2px 10px;
    border-radius: 2px;
  }

  .gamertag {
    color: $hng-text;
    font-weight: 700;
  }

  .arrow {
    color: $hng-gold;
  }

  .local-user {
    color: $hng-gold;
    font-weight: 700;
  }
}

.summary-currencies {
  display: flex;
  gap: 10px;
  margin: 12px 0;

  .cur {
    flex: 1;
    text-align: center;
    border: 1px solid rgba(200, 185, 138, 0.2);
    background: rgba(14, 16, 10, 0.7);
    padding: 8px 4px;

    .cur-label {
      display: block;
      color: $hng-text-dim;
      font-size: 11px;
      letter-spacing: 0.14em;
      margin-bottom: 3px;
    }

    .cur-val {
      color: $hng-gold;
      font-weight: 800;
      font-size: 15px;
    }
  }
}

.summary-counts {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 4px 16px;

  .count-row {
    display: flex;
    justify-content: space-between;
    font-size: 12.5px;
    color: $hng-text-dim;
    padding: 3px 0;
    border-bottom: 1px dotted rgba(200, 185, 138, 0.12);

    .count-val {
      color: $hng-text;
      font-weight: 600;
    }
  }
}

.summary-tip {
  margin: 12px 0 0;
  color: $hng-khaki;
  font-size: 12.5px;
  line-height: 1.6;

  b {
    color: $hng-gold;
  }
}
</style>
