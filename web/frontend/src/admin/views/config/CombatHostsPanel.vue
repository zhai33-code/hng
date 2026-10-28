<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { get, put } from '../../api'
import { fmtTime } from '../../format'

interface HostEntry {
  name: string
  addr: string
  public_addr: string
  max: number
  port_base: number
  port_max: number
  agent: string
  warserver: string
}

interface HostsResp {
  configured: boolean
  config: { hosts: HostEntry[] }
  updated_at: string | null
  updated_by: string
}

// 与后端 config_svc.COMBAT_HOSTS_DEFAULTS 保持一致
const DEFAULT_HOST: HostEntry = {
  name: '', addr: '', public_addr: '',
  max: 4, port_base: 5678, port_max: 5699,
  agent: '', warserver: '',
}

const loading = ref(true)
const saving = ref(false)
const configured = ref(false)
const updatedAt = ref<string | null>(null)
const updatedBy = ref('')
const hosts = reactive<HostEntry[]>([])

function addHost() {
  hosts.push({ ...DEFAULT_HOST, port_base: 5678, port_max: 5699 })
}

function removeHost(i: number) {
  hosts.splice(i, 1)
}

function applyConfig(cfg: { hosts: HostEntry[] }) {
  hosts.splice(0, hosts.length, ...cfg.hosts.map(h => ({ ...h })))
}

function resetDefaults() {
  hosts.splice(0, hosts.length, {
    ...DEFAULT_HOST,
    name: 'local', addr: '127.0.0.1',
  })
  ElMessage.info('已恢复默认值（尚未保存）')
}

/** 保存前本地校验（后端 pydantic 仍会兜底） */
function validate(): string[] {
  const errs: string[] = []
  const names = new Set<string>()
  hosts.forEach((h, i) => {
    const tag = h.name ? `「${h.name}」` : `第 ${i + 1} 行`
    if (!h.name) errs.push(`${tag}: 名称不能为空`)
    else if (names.has(h.name)) errs.push(`「${h.name}」: 名称重复`)
    else names.add(h.name)
    if (!h.addr) errs.push(`${tag}: 内网地址不能为空`)
    if (!(h.port_base >= 1024 && h.port_max <= 65535)
        || h.port_base > h.port_max)
      errs.push(`${tag}: 端口池 ${h.port_base}-${h.port_max} 非法`)
    else {
      const ports = h.port_max - h.port_base + 1
      if (h.max > 0 && ports < h.max)
        errs.push(`${tag}: 端口池(${ports} 个)小于容量(${h.max})`)
    }
    if (h.agent && !h.agent.startsWith('http://'))
      errs.push(`${tag}: agent 须为 http:// URL`)
  })
  if (!hosts.length) errs.push('至少需要一台主机')
  return errs
}

async function load() {
  loading.value = true
  try {
    const data = await get<HostsResp>('/config/combat_hosts')
    configured.value = data.configured
    updatedAt.value = data.updated_at
    updatedBy.value = data.updated_by
    applyConfig(data.config)
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '加载失败')
  } finally {
    loading.value = false
  }
}

async function save() {
  const errs = validate()
  if (errs.length) {
    ElMessageBox.alert(errs.slice(0, 8).join('<br>') +
      (errs.length > 8 ? `<br>… 共 ${errs.length} 处问题` : ''),
      '配置校验未通过', { type: 'error', dangerouslyUseHTMLString: true })
    return
  }
  try {
    await ElMessageBox.confirm(
      '保存后下一局成局按新主机表选机，已运行战局不受影响；' +
      '配置错误可能导致成局排队，请确认地址可达。', '保存配置',
      { type: 'warning' })
  } catch {
    return
  }
  saving.value = true
  try {
    await put('/config/combat_hosts', {
      hosts: hosts.map(h => ({
        ...h,
        public_addr: h.public_addr || h.addr,
      })),
    })
    ElMessage.success('已保存，下一局成局生效')
    await load()
  } catch (e: unknown) {
    ElMessage.error((e as Error).message || '保存失败')
  } finally {
    saving.value = false
  }
}

onMounted(load)
</script>

<template>
  <div v-loading="loading" class="hosts-panel">
    <div class="panel">
      <div class="head">
        <div class="head-left">
          <span class="head-title">战斗服主机</span>
          <el-tag :type="configured ? 'success' : 'info'" size="small">
            {{ configured ? '已保存 · 下一局成局生效' : '未保存过 · 当前按服务器环境变量回退' }}
          </el-tag>
        </div>
        <span v-if="configured" class="head-meta">
          上次保存 {{ fmtTime(updatedAt) }} · {{ updatedBy || '-' }}
        </span>
      </div>

      <el-alert class="alert" type="info" :closable="false" show-icon>
        每局战局在独立 combat server 进程上运行；成局时按各机剩余容量与端口池选机
        （取剩余容量最大者），agent 拉起失败的机器冷却 60 秒。保存即生效，无需重启。
        未保存时游戏服按单机环境变量（MM_MAX_COMBAT_SERVERS / MM_COMBAT_PORT_BASE /
        MM_COMBAT_PORT_MAX / HNG_COMBAT_WARSERVER）运行。
      </el-alert>

      <el-table :data="hosts" size="small" class="hosts-table">
        <el-table-column label="名称" width="120">
          <template #default="{ row }">
            <el-input v-model="row.name" placeholder="如 A机" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="内网地址" width="140">
          <template #default="{ row }">
            <el-input v-model="row.addr" placeholder="10.0.0.2" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="公网地址" width="140">
          <template #default="{ row }">
            <el-input v-model="row.public_addr" placeholder="缺省=内网地址" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="最大并发" width="100">
          <template #default="{ row }">
            <el-input-number v-model="row.max" :min="0" :max="64" size="small"
                             controls-position="right" style="width: 80px" />
          </template>
        </el-table-column>
        <el-table-column label="UDP 端口池" width="180">
          <template #default="{ row }">
            <div class="port-range">
              <el-input-number v-model="row.port_base" :min="1024" :max="65535"
                               size="small" controls-position="right" />
              <span class="port-dash">-</span>
              <el-input-number v-model="row.port_max" :min="1024" :max="65535"
                               size="small" controls-position="right" />
            </div>
          </template>
        </el-table-column>
        <el-table-column label="Agent" min-width="170">
          <template #default="{ row }">
            <el-input v-model="row.agent" placeholder="空=本机直接启动" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="warserver" min-width="140">
          <template #default="{ row }">
            <el-input v-model="row.warserver" placeholder="空=游戏服默认" size="small" />
          </template>
        </el-table-column>
        <el-table-column label="操作" width="70" fixed="right">
          <template #default="{ $index }">
            <el-button type="danger" link size="small"
                       :disabled="hosts.length <= 1"
                       @click="removeHost($index)">删除</el-button>
          </template>
        </el-table-column>
      </el-table>

      <div class="field-hints">
        <div>· 内网地址：该机在游戏服网络中的地址，用于握手来源 IP 匹配（远端机不能填 127.0.0.1）</div>
        <div>· 公网地址：客户端连接战局用的地址，客户端可直连时填公网 IP（缺省 = 内网地址）</div>
        <div>· 最大并发：0 = 不限（端口池自然封顶）；A 机配 4、B 机配 8 即按各机容量调度</div>
        <div>· Agent：http://ip:8888，远端机需常驻 combat_agent.py（token 用环境变量
          HNG_AGENT_TOKEN，须与游戏服一致）；本机留空</div>
        <div>· warserver：远端 combat server 回连游戏服的地址（NAT/跨网段时填公网入口，缺省 = 游戏服 env）</div>
      </div>

      <div class="btn-bar">
        <el-button plain @click="addHost">添加主机</el-button>
        <el-button plain @click="resetDefaults">恢复默认值</el-button>
        <el-button type="primary" :loading="saving" @click="save">保存配置</el-button>
      </div>
    </div>
  </div>
</template>

<style scoped lang="scss">
.hosts-panel {
  max-width: 1080px;
}

.head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 14px;
}

.head-left {
  display: flex;
  align-items: center;
  gap: 10px;
}

.head-title {
  font-weight: 700;
  font-size: 15px;
}

.head-meta {
  font-size: 12px;
  color: var(--el-text-color-secondary);
}

.alert {
  margin-bottom: 16px;
}

.hosts-table {
  margin-bottom: 10px;
}

.port-range {
  display: flex;
  align-items: center;
  gap: 4px;

  :deep(.el-input-number) {
    width: 76px;
  }
}

.port-dash {
  color: var(--el-text-color-secondary);
}

.field-hints {
  font-size: 12px;
  color: var(--el-text-color-secondary);
  line-height: 1.8;
  margin-bottom: 6px;
}

.btn-bar {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 14px;
}
</style>
