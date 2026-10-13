<template>
  <AdapterSettingsScaffold
    title="OneBot v11"
    description="管理 OneBot 实现的正向与反向 WebSocket 连接。每个账户拥有独立的连接状态、鉴权信息和会话命名空间。"
    :icon="Cable"
    :accounts="accounts"
    :selected="selected"
    :busy="busy"
    :dirty="dirty"
    :error="error"
    :message="message"
    :status-hint="statusHint"
    @select="selected = $event"
    @add="addAccount"
    @duplicate="duplicateAccount"
    @remove="removeAccount"
    @save="save"
    @reload="reload"
  >
    <template #account-meta="{ account }">{{ account.id }} · {{ modeLabel(account.connection_mode) }}</template>
    <template #default="{ account }">
      <div class="ob:space-y-5">
        <AdapterFormSection index="01" title="账户身份" description="用于在控制台、连接状态和会话中识别这个机器人。" :icon="Fingerprint">
          <label><span>显示名称</span><input v-model="account.name" placeholder="主机器人" @input="markDirty" /></label>
          <label><span>账户 ID</span><input v-model="account.id" placeholder="main" @input="markDirty" /><small>仅支持字母、数字、下划线和连字符。</small></label>
          <label class="setting-switch ob:md:col-span-2"><span><b>启用账户</b><small>保存后建立正向连接，或开始等待反向连接。</small></span><input v-model="account.enabled" type="checkbox" @change="markDirty" /></label>
        </AdapterFormSection>

        <AdapterFormSection index="02" title="连接与鉴权" description="设置 OneBot 实现的连接方向、地址与访问令牌。" :icon="Cable">
          <label><span>连接模式</span><select v-model="account.connection_mode" @change="markDirty"><option value="forward">正向 WebSocket</option><option value="reverse">反向 WebSocket</option><option value="both">正向 + 反向</option></select></label>
          <label><span>鉴权传递</span><select v-model="account.access_token_in_url" @change="markDirty"><option :value="true">URL 查询参数</option><option :value="false">Authorization Header</option></select></label>
          <label v-if="account.connection_mode !== 'reverse'" class="ob:md:col-span-2"><span>正向 WebSocket 地址</span><input v-model="account.ws_url" placeholder="ws://127.0.0.1:8080" @input="markDirty" /></label>
          <label class="ob:md:col-span-2"><span>访问令牌</span><div class="secret-field"><input v-model="account.access_token" :type="showSecret ? 'text' : 'password'" autocomplete="new-password" :placeholder="account.access_token_set ? '已设置，留空保持不变' : '请输入访问令牌'" @input="markDirty" /><button type="button" :title="showSecret ? '隐藏' : '显示'" @click="showSecret = !showSecret"><EyeOff v-if="showSecret" class="ob:h-4 ob:w-4" /><Eye v-else class="ob:h-4 ob:w-4" /></button></div></label>
          <div v-if="account.connection_mode !== 'forward'" class="ob:md:col-span-2 ob:rounded-lg ob:border ob:bg-muted/20 ob:p-4">
            <div class="ob:text-xs ob:font-medium">反向 WebSocket 地址</div>
            <div class="ob:mt-2 ob:flex ob:items-center ob:gap-2 ob:rounded-md ob:border ob:bg-background ob:px-3 ob:py-2"><code class="ob:min-w-0 ob:flex-1 ob:truncate ob:text-xs">{{ account.reverse_ws_path || `/onebot/v11/ws/${account.id}` }}</code><button class="ob:text-muted-foreground ob:transition-colors ob:hover:text-foreground" @click="copyText(account.reverse_ws_path || `/onebot/v11/ws/${account.id}`)"><Copy class="ob:h-4 ob:w-4" /></button></div>
            <p class="ob:mt-2 ob:text-xs ob:text-muted-foreground">当前反向连接：{{ account.status?.reverse_connections || 0 }}</p>
          </div>
        </AdapterFormSection>

        <AdapterFormSection index="03" title="平台与会话" description="控制消息进入核心后的平台标识和上下文隔离方式。" :icon="MessagesSquare">
          <label><span>平台名称</span><input v-model="account.platform_name" @input="markDirty" /></label>
          <label><span>会话前缀</span><input v-model="account.session_id_prefix" @input="markDirty" /></label>
          <label class="setting-switch"><span><b>群聊共享会话</b><small>同群成员共享上下文。</small></span><input v-model="account.use_group_as_session" type="checkbox" @change="markDirty" /></label>
          <label class="setting-switch"><span><b>包含 Bot ID</b><small>避免多个 Bot 在同群串话。</small></span><input v-model="account.session_id_include_bot_id" type="checkbox" @change="markDirty" /></label>
        </AdapterFormSection>

        <details class="ob:rounded-xl ob:border ob:bg-muted/10">
          <summary class="ob:cursor-pointer ob:px-4 ob:py-3 ob:text-sm ob:font-medium">高级连接参数</summary>
          <div class="ob:grid ob:grid-cols-1 ob:gap-4 ob:border-t ob:p-4 ob:sm:grid-cols-2">
            <label><span>初始重连（秒）</span><input v-model.number="account.reconnect_initial_delay" type="number" min="0.5" @input="markDirty" /></label>
            <label><span>最大重连（秒）</span><input v-model.number="account.reconnect_max_delay" type="number" min="1" @input="markDirty" /></label>
            <label><span>Ping 间隔</span><input v-model.number="account.ping_interval" type="number" min="1" @input="markDirty" /></label>
            <label><span>Ping 超时</span><input v-model.number="account.ping_timeout" type="number" min="1" @input="markDirty" /></label>
          </div>
        </details>
      </div>
    </template>
  </AdapterSettingsScaffold>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { Cable, Copy, Eye, EyeOff, Fingerprint, MessagesSquare } from 'lucide-vue-next'
import { AdapterFormSection, AdapterSettingsScaffold } from '../host'
import { loadAccounts, reloadAccounts, saveAccounts } from '../services/accounts'

type Account = Record<string, any>
const accounts = ref<Account[]>([])
const selected = ref(0)
const busy = ref(false)
const dirty = ref(false)
const showSecret = ref(false)
const error = ref('')
const message = ref('')
let timer: number | undefined
const current = computed(() => accounts.value[selected.value])
const statusHint = computed(() => current.value?.status?.reverse_connections ? `${current.value.status.reverse_connections} 个反向 WebSocket 已连接` : '连接状态会自动刷新')
const defaults = (): Account => ({ id: `onebot-${Date.now().toString(36)}`, name: '新 OneBot', enabled: false, connection_mode: 'forward', ws_url: 'ws://127.0.0.1:8080', access_token: '', access_token_in_url: true, platform_name: 'onebot', session_id_prefix: 'onebot', use_group_as_session: true, session_id_include_bot_id: true, reconnect_initial_delay: 2, reconnect_max_delay: 30, ping_interval: 20, ping_timeout: 10 })
function markDirty() { dirty.value = true; message.value = '' }
function modeLabel(mode = '') { return ({ forward: '正向', reverse: '反向', both: '双向' } as Record<string,string>)[mode] || mode }
async function fetchAccounts(silent = false) { if (!silent) busy.value = true; try { const fresh = await loadAccounts(); if (!dirty.value || !silent) accounts.value = fresh; error.value = '' } catch (e: any) { error.value = e?.response?.data?.detail || '无法读取 OneBot 配置，请确认适配器已安装并启用。' } finally { busy.value = false } }
function addAccount() { accounts.value.push(defaults()); selected.value = accounts.value.length - 1; markDirty() }
function duplicateAccount() { if (!current.value) return; const copy = structuredClone(current.value); copy.id = `${copy.id}-copy`; copy.name = `${copy.name} 副本`; copy.access_token = ''; copy.access_token_set = false; delete copy.status; accounts.value.push(copy); selected.value = accounts.value.length - 1; markDirty() }
function removeAccount() { if (accounts.value.length <= 1 || !confirm(`删除账户“${current.value.name || current.value.id}”？`)) return; accounts.value.splice(selected.value, 1); selected.value = Math.max(0, selected.value - 1); markDirty() }
async function save() { busy.value = true; try { accounts.value = await saveAccounts(accounts.value); selected.value = Math.min(selected.value, accounts.value.length - 1); dirty.value = false; message.value = '配置已保存，连接已热重载。'; error.value = '' } catch (e: any) { error.value = e?.response?.data?.detail || '保存失败' } finally { busy.value = false } }
async function reload() { busy.value = true; try { accounts.value = await reloadAccounts(); dirty.value = false; message.value = '连接已重载。'; error.value = '' } catch (e: any) { error.value = e?.response?.data?.detail || '重载失败' } finally { busy.value = false } }
async function copyText(text: string) { await navigator.clipboard.writeText(text); message.value = '地址已复制。' }
onMounted(async () => { await fetchAccounts(); timer = window.setInterval(() => fetchAccounts(true), 5000) })
onBeforeUnmount(() => window.clearInterval(timer))
</script>
