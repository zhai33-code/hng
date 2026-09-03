// 管理端路由（hash 模式：无需服务端 history 回退，静态托管即可）
import { createRouter, createWebHashHistory } from 'vue-router'
import { setUnauthorizedHandler } from './api'
import { useAuthStore } from './stores/auth'

export const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    {
      path: '/login',
      name: 'login',
      component: () => import('./views/LoginView.vue'),
      meta: { public: true },
    },
    {
      path: '/',
      redirect: '/dashboard',
    },
    {
      path: '/dashboard',
      name: 'dashboard',
      component: () => import('./views/DashboardView.vue'),
    },
    {
      path: '/players',
      name: 'players',
      component: () => import('./views/PlayersView.vue'),
    },
    {
      path: '/players/:id',
      name: 'player-detail',
      component: () => import('./views/PlayerDetailView.vue'),
    },
    {
      path: '/players/:id/characters/:cid',
      name: 'character-detail',
      component: () => import('./views/CharacterDetailView.vue'),
    },
    {
      path: '/battles',
      name: 'battles',
      component: () => import('./views/BattlesView.vue'),
    },
    {
      path: '/war',
      name: 'war',
      component: () => import('./views/WarView.vue'),
    },
    {
      path: '/reports',
      name: 'reports',
      component: () => import('./views/ReportsView.vue'),
    },
    {
      path: '/mail',
      name: 'mail',
      component: () => import('./views/MailView.vue'),
    },
    {
      path: '/vouchers',
      name: 'vouchers',
      component: () => import('./views/VouchersView.vue'),
    },
    {
      path: '/status',
      name: 'status',
      component: () => import('./views/StatusView.vue'),
    },
    {
      path: '/audit-log',
      name: 'audit-log',
      component: () => import('./views/AuditLogView.vue'),
    },
    { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
  ],
})

// 会话守卫：首次探测 /auth/me；未登录一律去 /login（login 页除外）
router.beforeEach(async (to) => {
  const auth = useAuthStore()
  if (!auth.checked) {
    await auth.fetchMe()
  }
  if (!to.meta.public && !auth.isLoggedIn) {
    return { name: 'login', query: { redirect: to.fullPath } }
  }
  if (to.name === 'login' && auth.isLoggedIn) {
    return { path: '/' }
  }
})

// 请求层 401（会话过期/被改密踢出）：清会话回登录页
setUnauthorizedHandler(() => {
  const auth = useAuthStore()
  auth.username = ''
  auth.checked = true
  if (router.currentRoute.value.name !== 'login') {
    router.push({ name: 'login' })
  }
})
