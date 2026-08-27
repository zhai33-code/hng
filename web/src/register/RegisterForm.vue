<template>
  <el-form
    ref="formRef"
    :model="form"
    :rules="rules"
    label-position="top"
    size="large"
    @submit.prevent
  >
    <el-form-item prop="username">
      <el-input
        v-model="form.username"
        placeholder="用户名（登录账号）"
        maxlength="32"
        autocomplete="username"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="password">
      <el-input
        v-model="form.password"
        type="password"
        placeholder="密码"
        show-password
        autocomplete="new-password"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="password2">
      <el-input
        v-model="form.password2"
        type="password"
        placeholder="确认密码"
        show-password
        autocomplete="new-password"
        :disabled="loading"
      />
    </el-form-item>
    <el-form-item prop="email">
      <el-input
        v-model="form.email"
        placeholder="邮箱（可选，用于找回密码）"
        maxlength="64"
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

const form = reactive({ username: '', password: '', password2: '', email: '' })

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
  email: [{ type: 'email', message: '邮箱格式不正确', trigger: 'blur' }],
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
      ...(form.email ? { email: form.email } : {}),
    })
    result.value = {
      type: 'success',
      message: '注册成功！现在可以启动游戏客户端，使用该账号登录了。',
    }
    form.username = form.password = form.password2 = form.email = ''
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

.submit-btn {
  width: 100%;
  margin-top: 6px;
}

.result-alert {
  margin-top: 18px;
}
</style>
