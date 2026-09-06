<script setup lang="ts">
// 动态配置页：el-tabs 容器，每个配置项一个 tab + views/config/ 下一个 Panel 组件
// 注意：el-tabs 未绑 v-model 时内部 currentName 默认 "0"（匹配不到任何 pane），
// 必须显式指定默认选中项，否则进页面无 tab 激活、面板内容不显示
import { ref } from 'vue'
import WeatherPanel from './config/WeatherPanel.vue'

const activeTab = ref('weather')
</script>

<template>
  <div class="config-view">
    <el-tabs v-model="activeTab">
      <el-tab-pane label="战局天气" name="weather">
        <WeatherPanel />
      </el-tab-pane>
    </el-tabs>
  </div>
</template>

<style scoped lang="scss">
.config-view {
  max-width: 860px;
}
</style>
