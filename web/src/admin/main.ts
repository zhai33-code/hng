import { createApp } from 'vue'
import { createPinia } from 'pinia'
import App from './App.vue'
import { router } from './router'
import '@/styles/hng-theme.scss'
import './styles/admin.scss'

createApp(App)
  .use(createPinia())
  .use(router)
  .mount('#app')
