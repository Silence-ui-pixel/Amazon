<script setup lang="ts">
import { computed, ref, watch } from 'vue';
import type { ProductTermAnalysis } from '../api/productTermAnalysis';
const props = defineProps<{ analysis: ProductTermAnalysis; busy?: boolean }>();
const emit = defineEmits<{ override: [ids: string[]]; reanalyze: []; evidence: [candidateId: string] }>();
function showEvidence(event: Event, item: ProductTermAnalysis['items'][number]) {
  if ((event.target as HTMLDetailsElement).open && item.evidence_loaded === false && !['waiting', 'running'].includes(item.status)) emit('evidence', item.candidate_id);
}
const selected = ref([...props.analysis.best_candidate_ids]);
const confidence = { high: '高可信', medium: '中可信', low: '低可信', none: '无有效样本' };
const score = (value: string | null) => (value === null ? '—' : Number(value).toFixed(2));
const winners = computed(() =>
  props.analysis.items.filter((item) => props.analysis.best_candidate_ids.includes(item.candidate_id)),
);
const winnerText = computed(() => winners.value.map((item) => item.text).join('、'));
const hasLowConfidenceWinner = computed(() =>
  winners.value.some((item) => item.summary?.confidence === 'low'),
);
const pending = computed(() => ['queued', 'running'].includes(props.analysis.status));
const phases = { waiting: '等待处理', searching: '读取搜索页', collecting: '采集商品详情', scoring: '模型评分', completed: '分析完成', failed: '分析失败' };
const itemStatus = (item: ProductTermAnalysis['items'][number]) =>
  item.status === 'waiting' ? '等待处理' : item.status === 'running' ? phases[item.phase ?? 'searching'] : item.status === 'failed' ? '分析失败' : '无有效样本';
const eligible = (item: ProductTermAnalysis['items'][number]) => item.status === 'completed' && Number(item.summary?.relevance_percent ?? 0) > 40;
const rejection = (item: ProductTermAnalysis['items'][number]) => item.rejection_reason ||
  (item.status === 'completed' && !eligible(item) ? `高相关比例 ${score(item.summary?.relevance_percent ?? null)}%，未严格大于40%` : item.status === 'no_samples' ? '没有有效评分样本，请查看商品剔除原因' : '');
const finishedCount = computed(
  () => props.analysis.items.filter((item) => ['completed', 'no_samples', 'failed'].includes(item.status)).length,
);
const statusText = computed(() =>
  pending.value
    ? `分析中 ${finishedCount.value}/${props.analysis.items.length}`
    : props.analysis.status === 'stale'
      ? '结果已过期'
      : ['failed', 'awaiting_review'].includes(props.analysis.status)
        ? '等待补充商品词'
        : props.analysis.complete
          ? '分析完成'
          : '结果不完整',
);
// 中断或空结果不能复用，允许运营显式触发重采。
const canReanalyze = computed(
  () =>
    !pending.value &&
    (!props.analysis.best_candidate_ids.length || props.analysis.status === 'incomplete'),
);
watch(
  () => props.analysis.best_candidate_ids,
  (value) => (selected.value = [...value]),
);
</script>

<template>
  <section class="term-analysis" aria-label="商品词相关度分析">
    <div class="analysis-heading">
      <div>
        <span class="eyebrow">自动分析结果</span>
        <h3>商品词相关度分析</h3>
      </div>
      <span class="status-badge" :class="{ incomplete: !analysis.complete && !pending }">{{ statusText }}</span>
    </div>
    <p class="analysis-counts" aria-live="polite">{{ finishedCount }} / {{ analysis.items.length }} 个词已处理，{{ analysis.best_candidate_ids.length }} 个达到门槛<span v-if="analysis.candidate_version"> · 候选第 {{ analysis.candidate_version }} 版</span></p>
    <p v-if="!pending && !analysis.best_candidate_ids.length" class="analysis-warning">请在上方修改或重新生成准确的商品词，再验证。已确认商品资料和历史分析会保留。</p>
    <div class="winner-card" :class="{ empty: !winnerText }">
      <span>系统纳入拓展的商品词</span>
      <strong>{{ winnerText || (pending ? '正在计算…' : '暂无可排名结果') }}</strong>
      <small v-if="winnerText">相关度大于 40% 的商品词会全部进入核心大词拓展</small>
      <small v-else>{{ pending ? '候选完成后自动纳入相关度大于 40% 的商品词' : '当前没有相关度大于 40% 的候选商品词' }}</small>
    </div>
    <p v-if="analysis.status === 'incomplete'" class="analysis-warning" role="alert">
      部分候选分析失败，当前选择仅基于已成功候选；失败候选没有按 0% 参与计算。
    </p>
    <p v-if="hasLowConfidenceWinner" class="analysis-warning">
      最终商品词的有效样本不足 10 个，可信度较低，建议展开证据人工核对。
    </p>
    <div class="weight-note">
      <span>用途 35%</span><span>款式结构 35%</span><span>规格 20%</span><span>价格 10%</span>
      <small>价格可比时使用四维权重；价格不可比时按前三维 35:35:20 归一。综合分严格大于 60 才计为高相关</small>
    </div>
    <div class="candidate-heading">
      <strong>候选商品词</strong><span>{{ analysis.items.length }} 个</span>
    </div>
    <details v-for="item in analysis.items" :key="item.candidate_id" class="candidate-card" @toggle="showEvidence($event, item)">
      <summary>
        <input v-model="selected" type="checkbox" :value="item.candidate_id" :disabled="pending || !eligible(item)" @click.stop />
        <span class="candidate-name">
          <strong>{{ item.text }}</strong>
          <small v-if="item.status === 'running'">{{ itemStatus(item) }} · 详情 {{ item.collected_products ?? 0 }}/{{ item.found_products ?? 0 }} · 评分 {{ item.scored_products ?? 0 }}/{{ item.collected_products ?? 0 }}</small>
          <small v-if="analysis.best_candidate_ids.includes(item.candidate_id)">系统推荐</small>
        </span>
        <span v-if="item.summary" class="candidate-metric">
          <strong>{{ score(item.summary.relevance_percent) }}%</strong>
          <small>{{ item.summary.high_related_count }} / {{ item.summary.valid_samples }} 高相关</small>
        </span>
        <span v-else class="candidate-metric failed">
          <strong>{{ itemStatus(item) }}</strong>
        </span>
        <span v-if="item.summary" class="confidence" :class="item.summary.confidence">
          {{ confidence[item.summary.confidence] }}
        </span>
      </summary>
      <p v-if="item.status === 'running'" class="live-progress" aria-live="polite">
        {{ itemStatus(item) }} · 搜索页 {{ item.search_pages ?? 0 }} 页 · 找到 {{ item.found_products ?? 0 }} 个自然商品 ·
        详情 {{ item.collected_products ?? 0 }}/{{ item.found_products ?? 0 }} · 采集失败 {{ item.collection_failures ?? 0 }} ·
        已评分 {{ item.scored_products ?? 0 }}/{{ item.collected_products ?? 0 }}
      </p>
      <p v-if="rejection(item)" class="rejection-reason">未入选原因：{{ rejection(item) }}</p>
      <div class="evidence-body">
      <p v-if="item.evidence_loaded === false && !['waiting', 'running'].includes(item.status)" role="status">正在读取本词的详细证据…</p>
      <p v-if="item.summary?.confidence === 'low'" class="evidence-warning">有效样本较少，建议人工核对证据后再使用。</p>
      <p v-if="item.collection" class="collection-summary">
        取得自然商品 {{ item.collection.unique_asins.length }} 个；搜索页原始自然卡片
        {{ item.collection.raw_natural_count }} 个。
      </p>
      <p v-if="item.collection?.pages.length">
        搜索证据：
        <a
          v-for="(page, index) in item.collection.pages"
          :key="page.url || index"
          :href="page.url"
          target="_blank"
          rel="noreferrer"
        >第 {{ index + 1 }} 页</a>
      </p>
      <p v-if="item.error" role="alert">{{ item.error }}</p>
      <div v-for="row in item.comparisons" :key="row.asin" class="comparison">
        <div class="comparison-title">
          <code>#{{ row.natural_position ?? '—' }} · {{ row.asin }}</code>
          <span :class="['relation-badge', row.status]">{{ row.status === 'high_related' ? '高相关' : row.status === 'excluded' ? '已剔除' : '低相关' }}</span>
          <strong v-if="row.overall_score">综合 {{ score(row.overall_score) }}</strong>
        </div>
        <div v-if="row.status !== 'excluded'" class="dimension-scores">
          <span>用途 {{ row.purpose_score }}</span><span>款式 {{ row.style_structure_score }}</span>
          <span>规格 {{ row.specifications_score }}</span><span>价格 {{ score(row.price_score) }}</span>
        </div>
        <small v-if="row.status !== 'excluded' && row.price_score === null" class="score-fallback">
          价格未参与，按用途 / 款式 / 规格三维归一评分
        </small>
        <p v-if="row.exclusion_reason" class="exclusion">剔除原因：{{ row.exclusion_reason }}</p>
        <p v-if="row.candidate_profile">用途：{{ row.candidate_profile.purpose.join('、') }}</p>
        <p v-if="row.candidate_profile">结构：{{ row.candidate_profile.style_structure.join('、') }}</p>
        <p v-if="row.candidate_profile">规格：{{ row.candidate_profile.specifications.join('、') }}</p>
        <small v-if="row.purpose_reason">用途依据：{{ row.purpose_reason }}</small>
        <small v-if="row.style_structure_reason">结构依据：{{ row.style_structure_reason }}</small>
        <small v-if="row.specifications_reason">规格依据：{{ row.specifications_reason }}</small>
        <small v-if="row.price_reason">价格依据：{{ row.price_reason }}</small>
      </div>
      </div>
    </details>
    <div class="manual-action">
      <span v-if="canReanalyze">可修改候选后验证；重采会再次调用采集与模型，未完成分析仅续跑失败项。</span>
      <span v-else>可从已通过 40% 门槛的候选中保存人工选择。</span>
      <button v-if="canReanalyze && analysis.items.length" type="button" :disabled="busy" @click="emit('reanalyze')">
        {{ busy ? '正在启动…' : analysis.status === 'incomplete' ? '继续未完成候选' : '重新采集并分析当前候选' }}
      </button>
      <button type="button" :disabled="!selected.length || busy || pending" @click="emit('override', selected)">保存人工选择</button>
    </div>
  </section>
</template>

<style scoped>
.term-analysis { margin-top: 16px; padding: 20px; border: 1px solid #cfe0d5; border-radius: 12px; background: #fff; }
.analysis-heading { display:flex; justify-content:space-between; align-items:flex-start; gap:16px; }
.analysis-heading h3 { margin:3px 0 0; font-size:18px; color:#243e34; }
.eyebrow { color:#67917d; font-size:11px; font-weight:700; letter-spacing:.08em; }
.status-badge { padding:5px 10px; border-radius:999px; color:#236a53; background:#edf7f1; font-size:12px; font-weight:700; }
.status-badge.incomplete { color:#9a5b13; background:#fff5e6; }
.winner-card { display:grid; gap:4px; margin:18px 0 12px; padding:17px 18px; border-radius:10px; color:#fff; background:linear-gradient(135deg,#226c53,#3b8b6c); }
.winner-card.empty { background:#64756c; }
.winner-card span,.winner-card small { font-size:12px; opacity:.84; }
.winner-card strong { font-size:22px; line-height:1.4; }
.analysis-warning { margin:8px 0; padding:9px 11px; border-radius:7px; color:#8a5416; background:#fff7e9; font-size:12px; }
.weight-note { display:flex; flex-wrap:wrap; gap:7px; align-items:center; margin:14px 0 20px; }
.weight-note span { padding:4px 8px; border-radius:5px; color:#466456; background:#eff5f1; font-size:11px; }
.weight-note small { color:#75887b; }
.candidate-heading { display:flex; justify-content:space-between; padding-bottom:9px; color:#40574b; font-size:13px; }
.candidate-card { border-top:1px solid #e4ebe6; }
.analysis-counts { color:#52685d; font-size:13px; }
.live-progress { margin:0 4px 12px 32px; color:#236a53; font-size:12px; }
.rejection-reason { margin:0 4px 12px 32px; color:#945a18; font-size:12px; }
.candidate-card summary { display:grid; grid-template-columns:auto minmax(130px,1fr) auto auto; gap:12px; align-items:center; padding:14px 4px; cursor:pointer; list-style:none; }
.candidate-card summary::-webkit-details-marker { display:none; }
.candidate-name { display:grid; gap:3px; }
.candidate-name small { color:#2e7c5d; font-size:10px; }
.candidate-metric { display:grid; gap:2px; text-align:right; }
.candidate-metric strong { color:#236a53; font-size:17px; }
.candidate-metric small { color:#7d8e84; font-size:10px; }
.candidate-metric.failed strong { color:#a15e22; font-size:12px; }
.confidence { min-width:54px; padding:4px 7px; border-radius:5px; text-align:center; font-size:10px; background:#edf6f1; color:#2d7559; }
.confidence.low,.confidence.none { background:#fff4e2; color:#945a18; }
.evidence-body { padding:2px 4px 16px 32px; }
.evidence-body>p { color:#63766a; font-size:12px; }
.evidence-body a { margin-left:8px; color:#267254; }
.evidence-warning,.exclusion { color:#9a5b13 !important; }
.comparison { display:grid; gap:7px; padding:11px 12px; font-size:12px; background:#f7faf8; border-radius:7px; margin-top:7px; }
.comparison-title { display:flex; flex-wrap:wrap; gap:10px; align-items:center; }
.comparison-title strong { margin-left:auto; color:#294d3e; }
.relation-badge { padding:2px 6px; border-radius:4px; font-size:10px; }
.relation-badge.high_related { color:#207052; background:#e6f4ec; }
.relation-badge.low_related { color:#81601f; background:#fff2d5; }
.relation-badge.excluded { color:#8c4f40; background:#f9eae6; }
.dimension-scores { display:flex; flex-wrap:wrap; gap:6px; }
.dimension-scores span { padding:3px 7px; border-radius:4px; color:#52685d; background:#eaf1ed; }
.score-fallback { color:#8a5b19 !important; }
.comparison p,.comparison small { margin:0; color:#60746a; }
.manual-action { display:flex; justify-content:space-between; gap:16px; align-items:center; margin-top:15px; padding-top:15px; border-top:1px solid #e4ebe6; color:#75887b; font-size:12px; }
.manual-action button { padding:8px 12px; border:1px solid #bcd3c5; border-radius:6px; color:#236a53; background:#f7fbf8; cursor:pointer; }
.manual-action button:disabled { opacity:.45; cursor:default; }
@media (max-width:640px) { .candidate-card summary { grid-template-columns:auto 1fr auto; } .confidence { grid-column:2 / -1; justify-self:start; } .manual-action { align-items:flex-start; flex-direction:column; } }
</style>
