<template>
  <div class="register-page">
    <div class="soldier-visual" aria-hidden="true"></div>
    <div class="register-panel">
      <header class="panel-head">
        <h1 class="hng-stencil">Heroes &amp; Generals</h1>
        <p class="subtitle">加入战斗 · 注册账号即可登录游戏客户端</p>
      </header>
      <RegisterForm />
      <footer class="panel-foot">
        <span>私服社区 · 非官方服务</span>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import RegisterForm from './RegisterForm.vue'
</script>

<style scoped lang="scss">
@use '@/styles/vars' as *;

.register-page {
  @include battlefield-bg;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
}

.soldier-visual {
  position: absolute;
  right: -6vw;
  bottom: 0;
  height: 96vh;
  aspect-ratio: 900 / 1500;
  background-image: url('/soldier.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: bottom right;
  filter: drop-shadow(0 0 40px rgba(0, 0, 0, 0.9));
  // 左缘渐变融入战场背景
  mask-image: linear-gradient(to right, transparent 0%, rgba(0, 0, 0, 0.25) 18%, #000 45%);
  pointer-events: none;
}

.register-panel {
  @include military-card;
  width: min(440px, 92vw);
  margin: 24px 8vw 24px auto;
  padding: 36px 40px 28px;
  position: relative;
  z-index: 1;
}

@media (max-width: 860px) {
  .soldier-visual {
    display: none;
  }
  .register-panel {
    margin: 24px auto;
  }
}

.panel-head {
  .subtitle {
    color: $hng-text-dim;
    margin: 10px 0 26px;
    font-size: 14px;
    letter-spacing: 0.05em;
  }
  h1 {
    margin: 0;
    font-size: 26px;
    line-height: 1.2;
  }
}

.panel-foot {
  margin-top: 22px;
  text-align: center;
  color: $hng-text-dim;
  font-size: 12px;
  letter-spacing: 0.08em;
}
</style>

<style lang="scss">
@use '@/styles/vars' as *;

// 注册页全局：暗色化 Element Plus 输入（非 scoped，作用于表单内组件）
.register-panel .el-input__wrapper,
.register-panel .el-textarea__inner {
  background: #191c12;
  box-shadow: 0 0 0 1px #3a3e2e inset;
}
.register-panel .el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px $hng-olive inset;
}
.register-panel .el-input__inner {
  color: $hng-text;
}
.register-panel .el-input__inner::placeholder {
  color: #6a6d5a;
}
.register-panel .el-form-item__error {
  color: #d97a6c;
}
</style>
