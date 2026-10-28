<template>
  <div class="join-panel" :style="{ width: `min(${width}, 92vw)` }">
    <div class="panel-accent" aria-hidden="true"></div>
    <header class="panel-head">
      <h1 class="hng-stencil">{{ title }}</h1>
      <p class="subtitle">{{ subtitle }}</p>
    </header>
    <p v-if="notice" class="notice">{{ notice }}</p>
    <slot />
    <div class="cross-nav">
      <span>{{ navHint }}</span>
      <RouterLink :to="navTo">{{ navLabel }} →</RouterLink>
    </div>
    <footer class="panel-foot">
      <span>私服社区 · 非官方服务 · 与官方服务器无关联</span>
    </footer>
  </div>
</template>

<script setup lang="ts">
withDefaults(
  defineProps<{
    title: string
    subtitle: string
    notice?: string
    width?: string
    navHint: string
    navLabel: string
    navTo: string
  }>(),
  { width: '430px' },
)
</script>

<style scoped lang="scss">
@use '@/styles/vars' as *;

.join-panel {
  @include military-card;
  margin: 24px auto;
  padding: 38px 42px 26px;
  position: relative;
  z-index: 1;

  // 卡片左缘金色强调线
  .panel-accent {
    position: absolute;
    left: 0;
    top: 18%;
    bottom: 18%;
    width: 3px;
    background: linear-gradient(to bottom, transparent, $hng-gold, transparent);
    opacity: 0.8;
  }
}

.panel-head {
  .subtitle {
    color: $hng-text-dim;
    margin: 10px 0 26px;
    font-size: 13px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
  }
  h1 {
    margin: 0;
    font-size: 30px;
    line-height: 1.2;
  }
}

.notice {
  color: $hng-khaki;
  font-size: 12.5px;
  line-height: 1.7;
  background: rgba(20, 24, 14, 0.6);
  border: 1px solid rgba(200, 185, 138, 0.16);
  border-left: 2px solid $hng-gold;
  padding: 10px 14px;
  margin: -8px 0 18px;
}

.panel-foot {
  margin-top: 22px;
  text-align: center;
  color: $hng-text-dim;
  font-size: 11px;
  letter-spacing: 0.08em;
  opacity: 0.7;
}

.cross-nav {
  margin-top: 18px;
  text-align: center;
  font-size: 12.5px;
  color: $hng-text-dim;

  a {
    color: $hng-gold;
    text-decoration: none;
    font-weight: 700;
    letter-spacing: 0.06em;

    &:hover {
      text-shadow: 0 0 10px rgba(232, 212, 139, 0.45);
    }
  }
}
</style>

<style lang="scss">
@use '@/styles/vars' as *;

// 入伍页全局：暗色化 Element Plus 输入（非 scoped，作用于表单内组件）
.join-panel .el-form-item__label {
  color: $hng-khaki;
  font-size: 12px;
  letter-spacing: 0.14em;
  text-transform: uppercase;
  font-weight: 700;
  padding-bottom: 4px;
}
.join-panel .el-input__wrapper,
.join-panel .el-textarea__inner {
  background: rgba(14, 16, 10, 0.85);
  box-shadow: 0 0 0 1px #3a3e2e inset;
  border-radius: 2px;
  transition: box-shadow 0.2s;
}
.join-panel .el-input__wrapper:hover {
  box-shadow: 0 0 0 1px rgba(200, 185, 138, 0.5) inset;
}
.join-panel .el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px $hng-olive inset, 0 0 14px rgba(138, 122, 46, 0.22);
}
.join-panel .el-input__inner {
  color: $hng-text;
  letter-spacing: 0.04em;
}
.join-panel .el-input__inner::placeholder {
  color: #6a6d5a;
}
.join-panel .el-form-item__error {
  color: #d97a6c;
}

// 表单共用元素（注册/迁移两表单同款）
.join-panel .submit-btn {
  width: 100%;
  margin-top: 8px;
  height: 46px;
  font-size: 16px;
  letter-spacing: 0.2em;
}
.join-panel .result-alert {
  margin-top: 18px;
}
.join-panel .section-title {
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

// 蜜罐字段：移出视口，正常用户不可交互
.join-panel .hp-field {
  position: absolute;
  left: -9999px;
  top: -9999px;
  width: 0;
  height: 0;
  opacity: 0;
  pointer-events: none;
}

.join-panel .captcha-box {
  display: flex;
  justify-content: center;
  min-height: 260px;
}
</style>
