// 入伍应用路由（history 模式：web_server.py 按路径枚举下发 join.html）
import { createRouter, createWebHistory } from 'vue-router'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/join',
      name: 'join',
      component: () => import('./views/JoinView.vue'),
      meta: { title: '新兵报到' },
    },
    {
      path: '/join/veteran',
      name: 'veteran',
      component: () => import('./views/VeteranView.vue'),
      meta: { title: '老兵归队' },
    },
    { path: '/', redirect: '/join' },
    { path: '/:pathMatch(.*)*', redirect: '/join' },
  ],
})

router.afterEach((to) => {
  document.title = `${to.meta.title ?? '入伍'} | Heroes & Generals 私服`
})
