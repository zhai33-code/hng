<script setup lang="ts">
import { reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, post } from '../api'

interface PlayerOption {
  id: number
  username: string
}

const sending = ref(false)
const form = reactive({
  mode: 'all' as 'all' | 'single',
  playerId: undefined as number | undefined,
  subject: '',
  body: '',
})

// 远程搜索玩家（复用 /players 接口）
const playerOptions = ref<PlayerOption[]>([])
const playerLoading = ref(false)

async function searchPlayers(q: string) {
  if (!q) {
    playerOptions.value = []
    return
  }
  playerLoading.value = true
  try {
    const r = await get<{ items: PlayerOption[] }>(
      `/players?page=1&page_size=20&q=${encodeURIComponent(q)}`)
    playerOptions.value = r.items
  } catch {
    playerOptions.value = []
  } finally {
    playerLoading.value = false
  }
}

async function onSend() {
  if (!form.subject.trim() || !form.body.trim()) {
    ElMessage.warning('标题和正文不能为空')
    return
  }
  if (form.mode === 'single' && !form.playerId) {
    ElMessage.warning('请选择收件玩家')
    return
  }
  const target = form.mode === 'all'
    ? '全体玩家（所有未删除账号）'
    : `玩家 #${form.playerId}`
  try {
    await ElMessageBox.confirm(
      `确认发送系统邮件给 ${target}？\n标题：${form.subject}`,
      '发送邮件', { type: 'warning' })
  } catch {
    return
  }
  sending.value = true
  try {
    const r = await post<{ ok: boolean; count: number }>('/mail/send', {
      player_id: form.mode === 'single' ? form.playerId : 0,
      subject: form.subject,
      body: form.body,
    })
    ElMessage.success(`已发送给 ${r.count} 位玩家`)
    form.subject = ''
    form.body = ''
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '发送失败')
  } finally {
    sending.value = false
  }
}
</script>

<template>
  <div class="panel mail-panel">
    <div class="form-hint">
      系统邮件直接写入玩家收件箱（sender_id=0），玩家在游戏内打开邮箱即可看到；
      已在线玩家需重新打开邮箱窗口刷新。全服邮件会给所有未删除账号各发一封。
    </div>

    <el-form label-width="90px" style="max-width: 720px">
      <el-form-item label="收件人">
        <el-radio-group v-model="form.mode">
          <el-radio value="all">全服玩家</el-radio>
          <el-radio value="single">指定玩家</el-radio>
        </el-radio-group>
      </el-form-item>
      <el-form-item v-if="form.mode === 'single'" label="选择玩家">
        <el-select v-model="form.playerId" filterable remote
                   :remote-method="searchPlayers" :loading="playerLoading"
                   placeholder="输入用户名搜索" style="width: 320px">
          <el-option v-for="p in playerOptions" :key="p.id"
                     :label="`${p.username} (#${p.id})`" :value="p.id" />
        </el-select>
      </el-form-item>
      <el-form-item label="标题">
        <el-input v-model="form.subject" maxlength="128" show-word-limit
                  placeholder="邮件标题" />
      </el-form-item>
      <el-form-item label="正文">
        <el-input v-model="form.body" type="textarea" :rows="8"
                  maxlength="4000" show-word-limit
                  placeholder="邮件正文（游戏内纯文本展示）" />
      </el-form-item>
      <el-form-item>
        <el-button type="primary" :loading="sending" @click="onSend">发送邮件</el-button>
      </el-form-item>
    </el-form>
  </div>
</template>

<style lang="scss" scoped>
.mail-panel {
  max-width: 860px;
}

.form-hint {
  font-size: 13px;
  color: #9a947e;
  margin-bottom: 18px;
  line-height: 1.6;
}
</style>
