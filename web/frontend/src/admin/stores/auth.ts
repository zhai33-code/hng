// 管理员会话 store：登录态只存用户名（凭证在 HttpOnly Cookie 里）
import { defineStore } from 'pinia'
import { get, post } from '../api'

export const useAuthStore = defineStore('auth', {
  state: () => ({
    username: '' as string,
    checked: false, // 启动后是否已探测过会话（路由守卫用）
  }),
  getters: {
    isLoggedIn: (s) => !!s.username,
  },
  actions: {
    /** 探测当前会话（/api/auth/me）；401 由 api 层静默处理。 */
    async fetchMe() {
      this.checked = true
      try {
        const r = await get<{ username: string }>('/auth/me')
        this.username = r.username
      } catch {
        this.username = ''
      }
    },
    async login(username: string, password: string) {
      const r = await post<{ ok: boolean; username: string }>('/auth/login', {
        username,
        password,
      }, { isLogin: true })
      this.username = r.username
    },
    async logout() {
      try {
        await post('/auth/logout')
      } catch {
        /* 登出请求失败也直接清本地态 */
      }
      this.username = ''
    },
  },
})
