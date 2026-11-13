<template>
  <section class="page" data-module="alert">
    <header class="page-head">
      <div>
        <h2>采集告警管理</h2>
        <p class="page-desc">维护采集异常的告警条件、分级通知与静默策略；启用规则的事件按聚合窗口合并，窗口结束统一发送摘要并支持恢复。</p>
      </div>
      <div class="page-actions">
        <button class="btn primary" type="button" @click="openCreate">登记告警规则</button>
        <button class="btn" type="button" @click="exportRows">导出采集告警清单</button>
      </div>
    </header>

    <div class="stat-row">
      <article v-for="item in stats" :key="item.label" class="stat-card">
        <span class="stat-label">{{ item.label }}</span>
        <strong class="stat-value">{{ item.value }}</strong>
      </article>
    </div>

    <form class="filter-bar" @submit.prevent="reload">
      <label v-for="field in filterFields" :key="field" class="filter-item">
        <span>{{ field }}</span>
        <input v-model="filters[field]" :placeholder="`按${field}检索`" />
      </label>
      <button class="btn" type="submit">查询</button>
      <button class="btn ghost" type="button" @click="resetFilters">重置条件</button>
    </form>

    <table class="data-table">
      <thead>
        <tr>
          <th v-for="column in columns" :key="column">{{ column }}</th>
          <th>可执行动作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="row in rows" :key="String(row.id)">
          <td v-for="column in columns" :key="column">{{ row[column] ?? '—' }}</td>
          <td class="row-actions">
            <button
              v-for="action in actions"
              :key="action"
              class="link"
              type="button"
              @click="runAction(action, row)"
            >
              {{ action }}
            </button>
          </td>
        </tr>
        <tr v-if="!rows.length">
          <td :colspan="columns.length + 1" class="empty-state">暂无采集告警数据，可先登记告警规则</td>
        </tr>
      </tbody>
    </table>

    <section class="engine-panel">
      <header class="panel-head">
        <div>
          <h3>告警聚合与恢复</h3>
          <p class="page-desc">仅「已启用且是否启用=是」的规则参与聚合；静默期内只累计，窗口结束生成一份摘要；恢复以原告警事件为目标，重复恢复只生效一次。</p>
        </div>
        <button class="btn ghost" type="button" @click="resetEngine">清空运行态</button>
      </header>

      <form class="filter-bar" @submit.prevent="ingestEvent">
        <label class="filter-item">
          <span>告警规则</span>
          <select v-model="engineForm.rule">
            <option value="" disabled>选择已启用规则</option>
            <option v-for="row in rows" :key="String(row.id)" :value="String(row['告警编号'])">
              {{ row['告警编号'] }}（{{ row['监控目标'] }} / {{ row['告警级别'] }} / {{ row['status'] }}）
            </option>
          </select>
        </label>
        <label class="filter-item">
          <span>事件时间</span>
          <input v-model="engineForm.occurred_at" type="datetime-local" />
        </label>
        <label class="filter-item filter-grow">
          <span>事件内容</span>
          <input v-model="engineForm.content" placeholder="如：TASK-0002 连续失败" />
        </label>
        <button class="btn primary" type="submit">受理事件</button>
        <label class="filter-item">
          <span>结算时间</span>
          <input v-model="tickAt" type="datetime-local" />
        </label>
        <button class="btn" type="button" @click="runTick">结算到期窗口</button>
      </form>

      <h4 class="table-title">聚合包（摘要按严重度各列 1 项）</h4>
      <table class="data-table">
        <thead>
          <tr>
            <th>聚合包</th><th>规则</th><th>类型</th><th>状态</th><th>监控目标</th><th>级别</th>
            <th>事件数</th><th>恢复项</th><th>摘要</th><th>发送次数</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="pkg in engineState.packages" :key="pkg.id">
            <td>{{ pkg.id }}</td>
            <td>{{ pkg.rule_code }}</td>
            <td>{{ pkg.kind === 'silence' ? '静默累计' : '窗口聚合' }}</td>
            <td><span class="badge" :class="`badge-${pkg.state}`">{{ stateText(pkg.state) }}</span></td>
            <td>{{ pkg['监控目标'] }}</td>
            <td>{{ pkg['告警级别'] }}</td>
            <td>{{ pkg.event_count }}</td>
            <td>{{ pkg.recovery_count }}</td>
            <td class="summary-cell">{{ pkg.summary?.text ?? '窗口未结束' }}</td>
            <td>{{ pkg.send_attempts }}</td>
            <td class="row-actions">
              <button
                v-if="pkg.event_ids.length"
                class="link"
                type="button"
                @click="recoverEvent(pkg.event_ids[0])"
              >
                恢复原事件
              </button>
              <button
                v-if="pkg.state === 'failed'"
                class="link"
                type="button"
                @click="retryPackage(pkg.id)"
              >
                从原包续发
              </button>
            </td>
          </tr>
          <tr v-if="!engineState.packages.length">
            <td colspan="11" class="empty-state">暂无聚合包，受理一条已启用规则的事件开始累计</td>
          </tr>
        </tbody>
      </table>

      <h4 class="table-title">工作台待处理项（恢复只关闭原告警事件对应的那一项）</h4>
      <table class="data-table">
        <thead>
          <tr><th>待处理项</th><th>聚合包</th><th>规则</th><th>监控目标</th><th>级别</th><th>状态</th><th>关闭原因</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in engineState.workbench" :key="item.id">
            <td>{{ item.id }}</td>
            <td>{{ item.package_id }}</td>
            <td>{{ item.rule_code }}</td>
            <td>{{ item.target }}</td>
            <td>{{ item.level }}</td>
            <td><span class="badge" :class="item.pending ? 'badge-open' : 'badge-sent'">{{ item.pending ? '待处理' : '已关闭' }}</span></td>
            <td>{{ item.close_reason ?? '—' }}</td>
          </tr>
          <tr v-if="!engineState.workbench.length">
            <td colspan="7" class="empty-state">暂无工作台项</td>
          </tr>
        </tbody>
      </table>

      <h4 class="table-title">历史通知（按发送当时的规则条件快照留档）</h4>
      <table class="data-table">
        <thead>
          <tr><th>通知</th><th>聚合包</th><th>规则</th><th>渠道</th><th>级别</th><th>状态</th><th>尝试次数</th><th>事件/恢复</th><th>摘要内容</th><th>条件快照</th></tr>
        </thead>
        <tbody>
          <tr v-for="ntf in engineState.notifications" :key="ntf.id">
            <td>{{ ntf.id }}</td>
            <td>{{ ntf.package_id }}</td>
            <td>{{ ntf.rule_code }}</td>
            <td>{{ ntf.channel }}</td>
            <td>{{ ntf.level }}</td>
            <td><span class="badge" :class="`badge-${ntf.status}`">{{ stateText(ntf.status) }}</span></td>
            <td>{{ ntf.attempts }}</td>
            <td>{{ ntf.event_count }} / {{ ntf.recovery_count }}</td>
            <td class="summary-cell">{{ ntf.summary_text ?? '—' }}</td>
            <td class="summary-cell">{{ formatSnapshot(ntf.snapshot) }}</td>
          </tr>
          <tr v-if="!engineState.notifications.length">
            <td colspan="10" class="empty-state">暂无历史通知</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span>共 {{ total }} 条采集告警记录</span>
      <span v-if="errorMessage" class="error-text">{{ errorMessage }}</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'

import { request } from '@/api/client'

type Row = Record<string, string | number | null>
type EngineState = {
  events: Row[]
  packages: Array<Record<string, any>>
  workbench: Array<Record<string, any>>
  notifications: Array<Record<string, any>>
  recoveries: Row[]
}

const ENDPOINT = '/api/alert'
const columns = ['告警编号', '监控目标', '告警条件', '告警级别', '通知渠道', '静默时段', '聚合窗口', '是否启用']
const actions = ['提交校验', '启用配置', '停用配置']
const statuses = ['草稿', '待校验', '已启用', '已停用']
const stats = [{ label: '告警规则总数', value: 0 }, { label: '待校验告警规则', value: 0 }, { label: '已启用告警规则', value: 0 }]

const rows = ref<Row[]>([])
const total = ref(0)
const errorMessage = ref('')
const filters = ref<Record<string, string>>({})
const filterFields = columns.slice(0, 3)

const engineState = reactive<EngineState>({ events: [], packages: [], workbench: [], notifications: [], recoveries: [] })
const engineForm = reactive({ rule: '', occurred_at: '', content: '' })
const tickAt = ref('')

function stateText(state: string): string {
  return { open: '累计中', ready: '待发送', sent: '已发送', failed: '发送失败' }[state] ?? state
}

function formatSnapshot(snapshot: Record<string, string> | null): string {
  if (!snapshot) return '—'
  return Object.entries(snapshot).map(([key, value]) => `${key}=${value}`).join('；')
}

function resetFilters() {
  filters.value = {}
  void reload()
}

function exportRows() {
  window.open(`${ENDPOINT}/export`, '_blank')
}

function openCreate() {
  errorMessage.value = '告警规则登记入口尚未接入审批流'
}

async function runAction(action: string, row: Row) {
  errorMessage.value = ''
  try {
    const response = await request(`${ENDPOINT}/${row.id}/actions`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    })
    if (!response.ok) {
      throw new Error('采集告警动作未生效，请稍后重试')
    }
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集告警操作失败'
  }
}

async function reload() {
  errorMessage.value = ''
  const query = new URLSearchParams(filters.value as Record<string, string>).toString()
  try {
    const response = await request(`${ENDPOINT}?${query}`)
    if (!response.ok) {
      throw new Error('告警规则列表读取失败')
    }
    const payload = await response.json()
    rows.value = payload.items ?? []
    total.value = payload.total ?? rows.value.length
    stats[0].value = payload.total ?? rows.value.length
    stats[1].value = rows.value.filter((item) => item.status === '待校验').length
    stats[2].value = rows.value.filter((item) => item.status === '已启用').length
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '采集告警列表读取失败'
  }
}

async function loadEngineState() {
  try {
    const response = await request(`${ENDPOINT}/engine/state`)
    if (!response.ok) {
      throw new Error('聚合引擎状态读取失败')
    }
    const payload = await response.json()
    engineState.events = payload.events ?? []
    engineState.packages = payload.packages ?? []
    engineState.workbench = payload.workbench ?? []
    engineState.notifications = payload.notifications ?? []
    engineState.recoveries = payload.recoveries ?? []
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '聚合引擎状态读取失败'
  }
}

async function postEngine(path: string, body: Record<string, unknown>) {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  const payload = await response.json().catch(() => ({}))
  if (!response.ok || payload.accepted === false || payload.effective === false || payload.ok === false) {
    const reason = payload.detail ?? payload.reason ?? payload.message ?? '聚合引擎操作未生效'
    throw new Error(String(reason))
  }
  return payload
}

async function ingestEvent() {
  errorMessage.value = ''
  if (!engineForm.rule) {
    errorMessage.value = '请先选择告警规则'
    return
  }
  try {
    await postEngine(`${ENDPOINT}/engine/events`, {
      values: {
        rule: engineForm.rule,
        content: engineForm.content,
        occurred_at: engineForm.occurred_at ? engineForm.occurred_at.replace('T', ' ') : null,
      },
    })
    engineForm.content = ''
    await loadEngineState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '事件受理失败'
  }
}

async function runTick() {
  errorMessage.value = ''
  try {
    await postEngine(`${ENDPOINT}/engine/tick`, {
      values: { now: tickAt.value ? tickAt.value.replace('T', ' ') : null },
    })
    await loadEngineState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '窗口结算失败'
  }
}

async function recoverEvent(eventId: string) {
  errorMessage.value = ''
  try {
    await postEngine(`${ENDPOINT}/engine/recoveries`, { values: { event_id: eventId } })
    await loadEngineState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '恢复操作失败'
  }
}

async function retryPackage(packageId: string) {
  errorMessage.value = ''
  try {
    await postEngine(`${ENDPOINT}/engine/packages/${packageId}/retry`, { values: {} })
    await loadEngineState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '续发失败'
  }
}

async function resetEngine() {
  errorMessage.value = ''
  try {
    await postEngine(`${ENDPOINT}/engine/reset`, {})
    await loadEngineState()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '清空运行态失败'
  }
}

onMounted(() => {
  void reload()
  void loadEngineState()
})
</script>

<style scoped>
.engine-panel {
  margin-top: 20px;
  background: #fff;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 14px 16px;
}
.panel-head {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  gap: 12px;
}
.panel-head h3 {
  margin: 0 0 4px;
}
.table-title {
  margin: 16px 0 8px;
  font-size: 14px;
}
.filter-grow {
  flex: 1;
  min-width: 220px;
}
.summary-cell {
  max-width: 320px;
  word-break: break-all;
}
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 12px;
  border: 1px solid var(--border);
}
.badge-open { background: #eff6ff; color: #1d4ed8; }
.badge-ready { background: #fff7ed; color: #c2410c; }
.badge-sent { background: #ecfdf3; color: #027a48; }
.badge-failed { background: #fef3f2; color: #b42318; }
</style>
