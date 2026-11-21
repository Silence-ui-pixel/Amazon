<script setup lang="ts">
import type { Verification } from '../api/verifications';
defineProps<{ task: Verification; busy?: boolean }>();
defineEmits<{ resume: []; review: [] }>();
const stages: Record<string, string> = {
  navigation: '加载搜索页',
  delivery: '确认配送地区',
  results: '读取搜索结果',
  screenshot: '保存证据截图',
};
// “范围内未找到”只表示本次有限搜索未命中，不代表 Amazon 全站没有该商品。
const labels: Record<string, string> = {
  queued: '等待执行',
  running: '核验中',
  matched: '已匹配',
  not_found_in_scope: '范围内未找到',
  failed: '核验失败',
  uncertain: '匹配不确定',
  needs_reference: '待指定主图',
  waiting: '等待核验',
  completed: '核验完成',
  incomplete: '核验未完成',
  interrupted: '已中断',
  paused: '等待处理访问检查',
};
</script>

<template>
  <section class="verification-panel" aria-label="所选词根搜索核验">
    <div class="verification-heading">
      <strong>搜索页核验 · 仅自然结果</strong>
      <span>{{ labels[task.status] || task.status }}</span>
    </div>
    <p>
      {{ task.message || '先搜索最小词根，范围内未命中再依次拓词；每个搜索词最多检查前三页。' }}
    </p>
    <p v-if="task.region">配送地区：{{ task.region }} · 按页码、页内自然位置比较</p>
    <div
      v-if="['paused', 'incomplete', 'interrupted'].includes(task.status)"
      class="recovery-actions"
    >
      <button
        :disabled="busy || ['opening', 'open'].includes(task.review_status || '')"
        @click="$emit('review')"
      >
        {{
          task.review_status === 'opening'
            ? '正在打开检查窗口…'
            : task.review_status === 'open'
              ? '检查窗口已打开'
              : '打开检查窗口'
        }}
      </button>
      <button
        :disabled="busy || task.model_outcome_pending || task.review_status === 'opening'"
        @click="$emit('resume')"
      >
        处理后继续
      </button>
      <p v-if="task.model_outcome_pending">
        上次模型调用结果待确认，已停止自动重试，避免重复计费。
      </p>
      <p v-else>在本机检查窗口完成访问确认，再点击继续。已完成的词根和页面会保留。</p>
    </div>
    <div class="verification-scroll">
      <table>
        <thead>
          <tr>
            <th>入库最小词根</th>
            <th>核验状态</th>
            <th>实际搜索词与自然位置</th>
            <th>匹配与证据</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="item in task.items" :key="item.candidate_id">
            <td>
              <strong>{{ item.text }}</strong
              ><small>{{ item.translation }}</small>
              <span v-if="task.best_candidate_ids.includes(item.candidate_id)" class="rank-badge">
                {{ task.final ? '位置最优参考' : '暂时最优' }}
              </span>
            </td>
            <td>
              {{ labels[item.status] || item.status
              }}<small>已检查 {{ item.checked_pages }} 页</small>
            </td>
            <td>
              <strong>{{ item.matched_query || item.active_query || item.text }}</strong>
              <small v-if="item.matched_query && item.matched_query !== item.text"
                >拓词命中；保存 {{ item.text }}</small
              >
              {{
                item.page && item.position ? `第 ${item.page} 页 · 自然第 ${item.position} 位` : '—'
              }}
            </td>
            <td>
              <span>{{ item.message }}</span
              ><small v-if="item.matched_asin">{{ item.matched_asin }}</small>
              <small
                v-if="item.failure_stage && ['failed', 'paused', 'uncertain'].includes(item.status)"
                >失败阶段：{{ stages[item.failure_stage] || item.failure_stage }}</small
              >
              <a
                v-if="item.diagnostic_url"
                :href="item.diagnostic_url"
                target="_blank"
                rel="noopener noreferrer"
                >诊断记录</a
              >
              <a
                v-if="item.search_url"
                :href="item.search_url"
                target="_blank"
                rel="noopener noreferrer"
                >搜索页</a
              >
              <a
                v-if="item.evidence_url"
                :href="item.evidence_url"
                target="_blank"
                rel="noopener noreferrer"
                >采集截图</a
              >
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <p>
      主图匹配只代表图片外观一致；ASIN
      匹配按输入商品编号判断。排名属于实际命中的搜索词；入库始终保存最小词根。搜索位置受地区与采集时间影响。
    </p>
  </section>
</template>

<style scoped>
.recovery-actions button {
  padding: 8px 14px;
  margin: 8px 10px 0 0;
  border: 1px solid #bfd4c8;
  border-radius: 7px;
  color: #174e3b;
  background: white;
  cursor: pointer;
}
.recovery-actions button:disabled {
  opacity: 0.5;
  cursor: default;
}
.verification-panel {
  margin: 20px 0;
  padding: 20px;
  border: 1px solid #dce5df;
  border-radius: 14px;
  background: #f8fbf9;
}
.verification-heading {
  display: flex;
  justify-content: space-between;
  gap: 16px;
  color: #174e3b;
}
p {
  color: #65736c;
  font-size: 12px;
  line-height: 1.7;
  margin: 10px 0;
}
.verification-scroll {
  overflow-x: auto;
}
table {
  width: 100%;
  border-collapse: collapse;
  min-width: 660px;
  font-size: 13px;
  text-align: left;
}
th {
  color: #637169;
  font-weight: 500;
  background: #eef4f0;
  padding: 12px;
}
td {
  padding: 14px 12px;
  border-bottom: 1px solid #e1e8e3;
  vertical-align: top;
}
td small {
  display: block;
  font-size: 11px;
  color: #748178;
  margin-top: 5px;
}
td a {
  display: inline-block;
  color: #26765b;
  margin: 8px 12px 0 0;
}
.rank-badge {
  display: inline-block;
  margin-top: 7px;
  padding: 3px 7px;
  background: #deeee4;
  color: #246646;
  border-radius: 5px;
  font-size: 11px;
}
</style>
