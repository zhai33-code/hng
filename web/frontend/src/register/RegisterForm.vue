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
        placeholder="至少 4 位"
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
</template>

<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElForm, ElInput, ElButton, ElAlert, FormRules, FormInstance } from 'element-plus'
import { apiPost, ApiError } from './api'

const formRef = ref<FormInstance>()
const loading = ref(false)
const result = ref<{ type: 'success' | 'error'; message: string } | null>(null)

const form = reactive({ username: '', password: '', password2: '' })

const rules: FormRules = {
  username: [
    { required: true, message: '请输入用户名', trigger: 'blur' },
    { pattern: /^[A-Za-z0-9_\-.]{2,24}$/, message: '2-24 位字母/数字/_-. 组合', trigger: 'blur' },
  ],
  password: [
    { required: true, message: '请输入密码', trigger: 'blur' },
    { min: 4, message: '密码至少 4 位', trigger: 'blur' },
  ],
  password2: [
    { required: true, message: '请再次输入密码', trigger: 'blur' },
    {
      validator: (_r, v, cb) => (v === form.password ? cb() : cb(new Error('两次输入的密码不一致'))),
      trigger: 'blur',
    },
  ],
}

async function submit() {
  if (loading.value) return
  const ok = await formRef.value?.validate().catch(() => false)
  if (!ok) return
  loading.value = true
  result.value = null
  try {
    await apiPost('/api/register', {
      username: form.username,
      password: form.password,
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
</style>
