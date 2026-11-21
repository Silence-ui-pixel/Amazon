<script setup lang="ts">
import { ref, watch } from 'vue';
import { request, type InputRun, type RootCandidates } from '../api/images';
import type { ProductTermAnalysis } from '../api/productTermAnalysis';

const props = defineProps<{ run: InputRun; candidates: RootCandidates; locked?: boolean }>();
const emit = defineEmits<{ updated: [result: RootCandidates] }>();
const editing = ref(false);
const busy = ref(false);
const error = ref('');
const draftVersion = ref(1);
const draft = ref<Array<{ text: string; translation: string; reason: string }>>([]);
const history = ref<ProductTermAnalysis[]>([]);
const base = () => `/api/v1/projects/${props.run.project_id}/pipeline-runs/${props.run.id}`;
function edit() {
  draftVersion.value = props.candidates.version ?? 1;
  draft.value = props.candidates.candidates.map(({ text, translation, reason }) => ({ text, translation, reason }));
  if (!draft.value.length) add();
  editing.value = true;
  error.value = '';
}
function add() { draft.value.push({ text: '', translation: '', reason: '人工补充的完整商品品类，需真实样本验证' }); }
async function update(regenerate = false) {
  if (busy.value || props.locked) return;
  busy.value = true;
  error.value = '';
  try {
    const result = await request<RootCandidates>(base() + '/product-term-candidates' + (regenerate ? '/regenerate' : ''), {
      method: regenerate ? 'POST' : 'PUT',
      body: JSON.stringify({ expected_version: regenerate ? props.candidates.version ?? 1 : draftVersion.value, ...(regenerate ? {} : { candidates: draft.value }) }),
    });
    editing.value = false;
    emit('updated', result);
  } catch (e) { error.value = e instanceof Error ? e.message : '候选保存失败'; }
  finally { busy.value = false; }
}
watch(() => [props.run.id, props.candidates.version], async () => {
  const runId = props.run.id;
  try {
    const result = await request<ProductTermAnalysis[]>(base() + '/product-term-analyses/history');
    if (props.run.id === runId) history.value = result;
  } catch { history.value = []; }
}, { immediate: true });
</script>

<template>
  <section class="candidate-manager" aria-label="候选商品词管理">
    <div class="manager-heading"><strong>候选商品词 · 第 {{ candidates.version ?? 1 }} 版</strong><span>{{ candidates.candidates.length }} 个候选</span></div>
    <p>优先完整、准确的品类，通常 3–5 个；例如 dumbbell rack、dumbbell stand。修改后保留已确认商品资料与历史分析。</p>
    <p v-if="locked" class="notice">当前分析或流程正在运行，暂停或结束后可修改候选。</p>
    <div class="actions">
      <button type="button" :disabled="busy || locked" @click="edit">修改 / 补充商品词</button>
      <button type="button" :disabled="busy || locked" @click="update(true)">{{ busy ? '处理中…' : '重新生成并验证（调用模型）' }}</button>
    </div>
    <form v-if="editing" @submit.prevent="update(false)">
      <div v-for="(item, index) in draft" :key="index" class="draft-row">
        <label>商品词<input v-model="item.text" required maxlength="100" :aria-label="`商品词 ${index + 1}`" placeholder="完整商品品类" /></label>
        <label>中文翻译<input v-model="item.translation" required maxlength="500" :aria-label="`中文翻译 ${index + 1}`" /></label>
        <label class="reason">推荐依据<input v-model="item.reason" required maxlength="500" :aria-label="`推荐依据 ${index + 1}`" /></label>
        <button type="button" :disabled="busy || draft.length <= 1" @click="draft.splice(index, 1)">删除</button>
      </div>
      <div class="actions">
        <button type="button" :disabled="busy || draft.length >= 10" @click="add">添加商品词</button>
        <button type="submit" :disabled="busy || locked">保存为新版本并验证</button>
        <button type="button" :disabled="busy" @click="editing = false">取消</button>
      </div>
      <small>会重新采集并评分当前版本候选；只有高相关商品比例严格大于 40% 才能进入下一步。</small>
    </form>
    <p v-if="error" role="alert" class="notice">{{ error }}</p>
    <details v-if="candidates.validation_issues?.length">
      <summary>生成质量检查 · {{ candidates.validation_issues.length }} 项</summary>
      <p v-for="(issue, index) in candidates.validation_issues" :key="index"><strong>{{ issue.text }}</strong>：{{ issue.message }}（{{ issue.code }}）</p>
    </details>
    <details v-if="candidates.raw_response">
      <summary>本版模型原始回复与诊断编号</summary>
      <p>诊断编号：{{ candidates.diagnostic_id }}</p><pre>{{ candidates.raw_response }}</pre>
    </details>
    <details v-if="candidates.history?.length || history.length">
      <summary>历史候选与分析记录</summary>
      <div v-for="version in candidates.history" :key="version.version">
        <p>第 {{ version.version ?? 1 }} 版：{{ version.candidates.map(item => item.text).join('、') || '全部未通过质量检查' }}</p>
        <p v-for="(issue, index) in version.validation_issues" :key="index">{{ issue.text }}：{{ issue.message }}</p>
        <details v-if="version.raw_response"><summary>模型原始回复</summary><pre>{{ version.raw_response }}</pre></details>
      </div>
      <details v-for="analysis in history" :key="analysis.id">
        <summary>第 {{ analysis.candidate_version ?? 1 }} 版分析 · {{ analysis.items.length }} 个词，{{ analysis.best_candidate_ids.length }} 个达标</summary>
        <p v-for="item in analysis.items" :key="item.candidate_id">{{ item.text }}：{{ item.summary ? `${item.summary.high_related_count}/${item.summary.valid_samples} 高相关（${item.summary.relevance_percent ?? '—'}%）` : item.error || '尚未完成' }}</p>
      </details>
    </details>
  </section>
</template>

<style scoped>
.candidate-manager { margin-top:16px; padding:18px; border:1px solid #cfe0d5; border-radius:10px; background:#f8fbf9; color:#40574b; font-size:13px; }
.manager-heading,.actions { display:flex; flex-wrap:wrap; gap:10px; justify-content:space-between; align-items:center; }
.actions { justify-content:flex-start; margin:12px 0; }
p { line-height:1.6; } button { padding:8px 10px; border:1px solid #bcd3c5; border-radius:6px; color:#236a53; background:white; cursor:pointer; }
button:disabled { opacity:.45; cursor:default; } .draft-row { display:flex; flex-wrap:wrap; align-items:end; gap:8px; margin:12px 0; }
label { display:grid; gap:4px; flex:1; min-width:140px; } .reason { flex:2; } input { width:100%; box-sizing:border-box; padding:8px; border:1px solid #bcd3c5; border-radius:4px; }
.notice { color:#8a5416; } details { margin-top:12px; } summary { cursor:pointer; } pre { white-space:pre-wrap; overflow-wrap:anywhere; max-height:300px; overflow:auto; font-size:12px; }
</style>
