<template>
  <JoinPanel
    title="新兵报到"
    subtitle="注册账号 · 即刻加入战斗"
    nav-hint="已有外服账号？"
    nav-label="从外服迁移数据"
    nav-to="/join/veteran"
  >
    <RegisterForm />
  </JoinPanel>
</template>

<script setup lang="ts">
import JoinPanel from '../JoinPanel.vue'
import RegisterForm from '../RegisterForm.vue'
</script>
