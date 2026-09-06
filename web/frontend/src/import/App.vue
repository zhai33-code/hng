<template>
  <div class="import-page">
    <!-- 顶部标题带 -->
    <div class="top-bar" aria-hidden="true">
      <div class="top-bar-inner">
        <span class="star">★</span>
        <span class="top-title">Heroes &amp; Generals</span>
        <span class="top-sub">PRIVATE SERVER</span>
      </div>
    </div>

    <!-- 右侧士兵立绘 -->
    <div class="soldier-visual" aria-hidden="true"></div>
    <!-- 底部阵营三色条 -->
    <div class="faction-strip" aria-hidden="true">
      <i class="us"></i><i class="ge"></i><i class="su"></i>
    </div>

    <div class="import-panel">
      <div class="panel-accent" aria-hidden="true"></div>
      <header class="panel-head">
        <h1 class="hng-stencil">老兵归队</h1>
        <p class="subtitle">外服账号迁移 · 数据一步带入本服</p>
      </header>
      <p class="notice">
        使用外服（官方服务器）账号密码登录，系统将抓取你的士兵、装备与货币数据，
        并创建一个新的本服账号。每个外服账号只能迁移一次。
      </p>
      <ImportForm />
      <div class="cross-nav">
        <span>不需要迁移？</span>
        <a href="/register">直接注册新账号 →</a>
      </div>
      <footer class="panel-foot">
        <span>私服社区 · 非官方服务 · 与官方服务器无关联</span>
      </footer>
    </div>
  </div>
</template>

<script setup lang="ts">
import ImportForm from './ImportForm.vue'
</script>

<style scoped lang="scss">
@use '@/styles/vars' as *;

.import-page {
  @include battlefield-bg;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  background-color: #101208;
}

// ---- 顶部标题带 ----
.top-bar {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  z-index: 2;
  background: linear-gradient(to bottom, rgba(10, 11, 7, 0.95), rgba(10, 11, 7, 0));
  border-bottom: 1px solid rgba(200, 185, 138, 0.18);

  .top-bar-inner {
    max-width: 1200px;
    margin: 0 auto;
    padding: 14px 28px;
    display: flex;
    align-items: center;
    gap: 14px;
  }

  .star {
    color: $hng-gold;
    font-size: 18px;
    text-shadow: 0 0 12px rgba(232, 212, 139, 0.5);
  }

  .top-title {
    font-weight: 800;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: $hng-text;
    font-size: 14px;
  }

  .top-sub {
    margin-left: auto;
    color: $hng-text-dim;
    font-size: 11px;
    letter-spacing: 0.28em;
  }
}

// ---- 士兵立绘 ----
.soldier-visual {
  position: absolute;
  right: 6vw;
  bottom: 0;
  height: 92vh;
  aspect-ratio: 900 / 1500;
  background-image: url('/soldier.png');
  background-size: contain;
  background-repeat: no-repeat;
  background-position: bottom right;
  filter: drop-shadow(0 0 40px rgba(0, 0, 0, 0.9)) saturate(0.7);
  opacity: 0.55;
  mask-image: linear-gradient(to right, transparent 0%, rgba(0, 0, 0, 0.2) 25%, #000 55%);
  pointer-events: none;
}

// ---- 底部阵营三色条 ----
.faction-strip {
  position: absolute;
  left: 0;
  right: 0;
  bottom: 0;
  display: flex;
  height: 4px;
  z-index: 2;

  i {
    flex: 1;

    &.us { background: linear-gradient(to right, $hng-blue, rgba($hng-blue, 0.3)); }
    &.ge { background: linear-gradient(to right, $hng-grey, rgba($hng-grey, 0.3)); }
    &.su { background: linear-gradient(to right, $hng-red, rgba($hng-red, 0.3)); }
  }
}

// ---- 迁移卡片 ----
.import-panel {
  @include military-card;
  width: min(470px, 92vw);
  margin: 24px auto;
  padding: 36px 42px 24px;
  position: relative;
  z-index: 1;

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

@media (max-width: 860px) {
  .soldier-visual {
    display: none;
  }
  .top-sub {
    display: none;
  }
}

.panel-head {
  .subtitle {
    color: $hng-text-dim;
    margin: 10px 0 18px;
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
  margin: 0 0 18px;
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

// 迁移页全局：暗色化 Element Plus 输入（与 register 页同款）
.import-panel .el-input__wrapper,
.import-panel .el-textarea__inner {
  background: rgba(14, 16, 10, 0.85);
  box-shadow: 0 0 0 1px #3a3e2e inset;
  border-radius: 2px;
  transition: box-shadow 0.2s;
}
.import-panel .el-input__wrapper:hover {
  box-shadow: 0 0 0 1px rgba(200, 185, 138, 0.5) inset;
}
.import-panel .el-input__wrapper.is-focus {
  box-shadow: 0 0 0 1px $hng-olive inset, 0 0 14px rgba(138, 122, 46, 0.22);
}
.import-panel .el-input__inner {
  color: $hng-text;
  letter-spacing: 0.04em;
}
.import-panel .el-input__inner::placeholder {
  color: #6a6d5a;
}
.import-panel .el-form-item__error {
  color: #d97a6c;
}
</style>
