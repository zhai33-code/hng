<script setup lang="ts">
import { computed } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessageBox } from 'element-plus'
import { useAuthStore } from './stores/auth'
import { router } from './router'

const auth = useAuthStore()
const route = useRoute()

const isLogin = computed(() => route.name === 'login')

const titles: Record<string, string> = {
  dashboard: '仪表盘',
  players: '玩家管理',
  'player-detail': '玩家详情',
  'character-detail': '角色装备明细',
  'login-events': '登录记录',
  battles: '战报',
  war: '战争（RTS）',
  reports: '举报处理',
  mail: '发送邮件',
  vouchers: '兑换码',
  config: '服务器配置',
  status: '服务器状态',
  'audit-log': '审计日志',
}
const pageTitle = computed(() => titles[String(route.name)] || '管理后台')

async function onLogout() {
  try {
    await ElMessageBox.confirm('确定退出登录？', '确认', { type: 'warning' })
  } catch {
    return
  }
  await auth.logout()
  router.push({ name: 'login' })
}
</script>

<template>
  <div v-if="isLogin" class="login-wrap">
    <RouterView />
  </div>
  <div v-else class="admin-layout">
    <aside class="admin-aside">
      <div class="admin-brand">
        <span class="brand-title">HnG Admin</span>
        <span class="brand-sub">Private Server</span>
      </div>
      <el-menu class="admin-menu" :default-active="route.path" router>
        <el-menu-item index="/dashboard">仪表盘</el-menu-item>
        <el-menu-item index="/players">玩家管理</el-menu-item>
        <el-menu-item index="/login-events">登录记录</el-menu-item>
        <el-menu-item index="/battles">战报</el-menu-item>
        <el-menu-item index="/war">战争（RTS）</el-menu-item>
        <el-menu-item index="/reports">举报处理</el-menu-item>
        <el-menu-item index="/mail">发送邮件</el-menu-item>
        <el-menu-item index="/vouchers">兑换码</el-menu-item>
        <el-menu-item index="/config">服务器配置</el-menu-item>
        <el-menu-item index="/status">服务器状态</el-menu-item>
        <el-menu-item index="/audit-log">审计日志</el-menu-item>
      </el-menu>
      <div class="admin-user">
        <span>{{ auth.username }}</span>
        <el-button size="small" text @click="onLogout">退出</el-button>
      </div>
    </aside>
    <main class="admin-main">
      <header class="admin-header">
        <span class="page-title">{{ pageTitle }}</span>
      </header>
      <div class="admin-body">
        <RouterView :key="route.fullPath" />
      </div>
    </main>
  </div>
</template>

<style lang="scss">
.login-wrap {
  height: 100vh;
}
</style>
