<template>
  <JoinPanel
    title="老兵归队"
    subtitle="外服账号迁移 · 数据一步带入本服"
    width="470px"
    notice="使用外服（官方服务器）账号密码登录，系统将抓取你的士兵、装备与货币数据，并创建一个新的本服账号。每个外服账号只能迁移一次。"
    nav-hint="不需要迁移？"
    nav-label="直接注册新账号"
    nav-to="/join"
  >
    <ImportForm />
  </JoinPanel>
</template>

<script setup lang="ts">
import JoinPanel from '../JoinPanel.vue'
import ImportForm from '../ImportForm.vue'
</script>
