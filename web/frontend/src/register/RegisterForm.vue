<template>
  <el-form
    ref="formRef"
    :model="form"
    :rules="rules"
    label-position="top"
    size="large"
    @submit.prevent
  >
    <el-form-item prop="username" label="用户名">
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

    <!-- 蜜罐：正常用户不可见，机器人填写即拒绝注册 -->
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
      {{ loading ? '正在部署…' : '加入战斗' }}
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
import { reactive, ref } from 'vue'
import {
  ElForm, ElInput, ElButton, ElAlert, ElDialog, ElMessage,
  FormRules, FormInstance,
} from 'element-plus'
import { Slide } from 'go-captcha-vue'
import 'go-captcha-vue/dist/style.css'
import { apiGet, apiPost, ApiError } from './api'

// go-captcha-vue SlideData（与后端 /api/captcha/get 返回逐字段对应）
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

const formRef = ref<FormInstance>()
const loading = ref(false)
const result = ref<{ type: 'success' | 'error'; message: string } | null>(null)

const form = reactive({
  username: '',
  password: '',
  password2: '',
  website: '',    // 蜜罐
  nickname2: '',  // 蜜罐
})

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
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

// ---- 验证码 ----

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
  // 挑战一次性：校验失败需重新出题
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
    await doRegister(r.token)
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

async function doRegister(token: string) {
  loading.value = true
  result.value = null
  try {
    await apiPost('/api/register', {
      username: form.username,
      password: form.password,
      token,
      website: form.website,
      nickname2: form.nickname2,
    })
    result.value = {
      type: 'success',
      message: '注册成功！现在可以启动游戏客户端，使用该账号登录了。',
    }
    form.username = form.password = form.password2 = ''
  } catch (e) {
    const err = e as ApiError
    result.value = { type: 'error', message: err.message || '注册失败，请稍后重试' }
  } finally {
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

// 蜜罐字段：移出视口，正常用户不可交互
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
</style>
