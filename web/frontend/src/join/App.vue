<template>
  <div class="join-page">
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

    <RouterView />
  </div>
</template>

<style scoped lang="scss">
@use '@/styles/vars' as *;

.join-page {
  @include battlefield-bg;
  min-height: 100vh;
  display: flex;
  align-items: center;
  justify-content: center;
  position: relative;
  overflow: hidden;
  background-color: #101208; // $hng-bg-deep 再压暗一档，突出中央战场
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

// ---- 士兵立绘（背景装饰，退到表单后方） ----
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

@media (max-width: 860px) {
  .soldier-visual {
    display: none;
  }
  .top-sub {
    display: none;
  }
}
</style>
