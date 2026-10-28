// 滑动验证码组合式：出题 / 校验 / 失败重出全流程，注册与迁移表单共用。
// 表单只管 submit 时 open()，验证通过后回调 onVerified(token)。
import { reactive, ref } from 'vue'
import { ElMessage } from 'element-plus'
import { apiGet, apiPost, ApiError } from './api'

// go-captcha-vue SlideData（与后端 /api/captcha/get 返回逐字段对应）
export interface SlideData {
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

export function useSlideCaptcha(onVerified: (token: string) => Promise<void> | void) {
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
      await onVerified(r.token)
    } catch (e) {
      const err = e as ApiError
      ElMessage.error(err.message || '验证失败，请重试')
      reset()
      loadCaptcha()
    } finally {
      capChecking.value = false
    }
  }

  function openCaptcha() {
    captchaVisible.value = true // el-dialog @open 触发 loadCaptcha
  }

  return { captchaVisible, capLoading, capRef, slideData, capEvents, loadCaptcha, openCaptcha }
}
