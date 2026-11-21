<script setup lang="ts">
import { usePanelSync } from '../panelSync';
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { competitorApi, type Competitors, type Review } from '../api/competitors';
import { coreApi } from '../api/coreTerms';
import type { InputRun } from '../api/images';

const props = defineProps<{
  run: InputRun;
  syncRevision?: number;
  coreReady: boolean;
  coreSelectionId: string;
  marketplace: string;
}>();
const emit = defineEmits<{
  status: [value: 'waiting' | 'running' | 'review' | 'done'];
  selection: [id: string];
  saved: [];
}>();
const workspace = ref<Competitors | null>(null);
const reviews = ref<Review[]>([]);
const keywords = ref<string[]>([]);
const manual = ref('');
const sourceLabel = ref('');
const csvText = ref('');
const referencePrice = ref('');
const currency = ref('USD');
// 页面用百分数填写价格容差；不能把“15”直接作为后端的比例 0.15 使用。
const tolerance = ref('15');
const busy = ref('');
// Read-only restoration must not change saved execution status.
const reading = ref(true);
const error = ref('');
const dirty = ref(false);
const bulkReason = ref('');
const bulkPriceNote = ref('');
const bulkFeedback = ref('');
const decidedCount = computed(() => reviews.value.filter((x) => x.decision !== 'pending').length);
/** 只补齐已判断候选的空白说明；不能代替人工保留，也不能覆盖逐项核对记录。 */
function fillBlankNotes() {
  if (busy.value || !props.coreReady || !workspace.value || workspace.value.stale) return;
  let reasons = 0;
  let prices = 0;
  for (const review of reviews.value) {
    if (review.decision === 'pending') continue;
    if (!review.reason.trim() && bulkReason.value.trim()) {
      review.reason = bulkReason.value.trim();
      reasons++;
    }
    if (
      review.decision === 'include' &&
      !review.price_override_reason.trim() &&
      bulkPriceNote.value.trim()
    ) {
      review.price_override_reason = bulkPriceNote.value.trim();
      prices++;
    }
  }
  if (reasons || prices) {
    dirty.value = true;
    error.value = '';
  }
  bulkFeedback.value = `已填充 ${reasons} 条核对理由、${prices} 条价格说明，请点击保存竞对选择。`;
}
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
});
const kept = computed(() => reviews.value.filter((x) => x.decision === 'include').length);
const saved = computed(
  () =>
    !!workspace.value?.confirmed_at && !workspace.value.stale && !dirty.value && props.coreReady,
);
watch(
  () => [workspace.value?.id ?? '', reading.value] as const,
  ([id, loading]) => { if (!loading) emit('selection', id); },
  { immediate: true },
);
watch(
  [saved, reading, busy, workspace, error, () => props.coreReady],
  () => {
    if (reading.value) return;
    emit(
      'status',
      busy.value
        ? 'running'
        : saved.value
          ? 'done'
          : workspace.value || error.value
            ? 'review'
            : 'waiting',
    );
  },
  { immediate: true },
);
function apply(data: Competitors | null) {
  bulkReason.value = '';
  bulkPriceNote.value = '';
  bulkFeedback.value = '';
  workspace.value = data;
  reviews.value =
    data?.items.map(({ asin, decision, reason, price_override_reason }) => ({
      asin,
      decision,
      reason,
      price_override_reason,
    })) ?? [];
  referencePrice.value = data?.reference_price ?? '';
  currency.value =
    data?.currency ??
    ({ CA: 'CAD', UK: 'GBP', JP: 'JPY', DE: 'EUR', FR: 'EUR', IT: 'EUR', ES: 'EUR' }[
      props.marketplace
    ] ||
      'USD');
  tolerance.value = String(Number(data?.tolerance ?? '0.15') * 100);
  dirty.value = false;
}
async function perform(label: string, action: () => Promise<Competitors | null>) {
  if (busy.value) return;
  const current = ++epoch;
  reading.value = label === '读取中';
  busy.value = label;
  error.value = '';
  try {
    const data = await action();
    if (alive && current === epoch) {
      apply(data);
      if (
        label === '保存中' ||
        label === '发现候选中' ||
        label === '商机 CSV 下载并导入中' ||
        label === '全部核心词商机 CSV 下载并筛选中'
      )
        emit('saved');
    }
  } catch (e) {
    if (alive && current === epoch) error.value = e instanceof Error ? e.message : '竞对操作失败';
  } finally {
    if (alive && current === epoch) { busy.value = ''; reading.value = false; }
  }
}
async function reload() {
  const run = props.run;
  await perform('读取中', async () => {
    const [data, core] = await Promise.all([competitorApi.get(run), coreApi.selection(run)]);
    keywords.value = core?.items.map((x) => x.keyword) ?? [];
    return data;
  });
}
watch(
  () => props.run.id,
  () => {
    epoch++;
    busy.value = '';
    apply(null);
    void reload();
  },
  { immediate: true },
);
const syncPending = usePanelSync(
  () => [props.syncRevision, props.coreSelectionId],
  () =>
    !!busy.value ||
    dirty.value ||
    !!manual.value ||
    !!csvText.value ||
    !!bulkReason.value ||
    !!bulkPriceNote.value,
  reload,
);
const version = () => workspace.value?.version ?? 0;
function discover(source: 'manual' | 'front_search' | 'poe_csv') {
  const asins = manual.value
    .toUpperCase()
    .split(/[\s,，;；]+/)
    .filter(Boolean);
  if (source === 'manual' && (!asins.length || asins.some((x) => !/^[A-Z0-9]{10}$/.test(x)))) {
    error.value = '请输入 10 位字母或数字 ASIN，以空格、逗号或换行分隔';
    return;
  }
  if (source === 'front_search' && !keywords.value.length) {
    error.value = '请先保存核心大词';
    return;
  }
  void perform('发现候选中', () =>
    competitorApi.discover(props.run, {
      source,
      expected_version: version(),
      asins: source === 'manual' ? asins : [],
      keywords: [],
      csv_text: source === 'poe_csv' ? csvText.value : '',
      source_label: sourceLabel.value,
    }),
  );
}
function downloadPoe() {
  if (!keywords.value.length) {
    error.value = '请先保存第二步已确认的核心大词';
    return;
  }
  void perform('全部核心词商机 CSV 下载并筛选中', () =>
    competitorApi.poeDownloadAll(props.run, {
      expected_version: version(),
      retry_keywords: [],
    }),
  );
}
function retryPoe(keyword: string) {
  void perform(`重试 ${keyword} 中`, () =>
    competitorApi.poeDownloadAll(props.run, {
      expected_version: version(),
      retry_keywords: [keyword],
    }),
  );
}
const stageLabels: Record<string, string> = {
  search: '搜索核心大词并读取购买意图',
  archive: '读取已归档商品表',
  selection: '选择购买意图',
  parse: '校验 30 天商品表',
  intent_collection: '读取购买意图',
  intent_selection: '选择购买意图',
  download: '下载最近 30 天商品表',
  csv_validation: '校验 30 天商品表',
};
async function readCsv(event: Event) {
  const input = event.target;
  if (!(input instanceof HTMLInputElement)) return;
  const file = input.files?.[0];
  if (!file) return;
  if (file.size > 2_000_000) {
    error.value = 'CSV 文件须小于 2 MB';
    return;
  }
  const current = epoch;
  try {
    const text = await file.text();
    if (alive && current === epoch) {
      csvText.value = text;
      sourceLabel.value = file.name;
    }
  } catch {
    if (alive && current === epoch) error.value = '文件读取失败，请重新选择';
  }
}
function details(asin: string) {
  void perform('读取商品详情中', () =>
    competitorApi.discover(props.run, {
      source: 'details',
      asins: [asin],
      expected_version: version(),
    }),
  );
}
function save() {
  if (
    referencePrice.value &&
    (!Number.isFinite(Number(referencePrice.value)) || Number(referencePrice.value) <= 0)
  ) {
    error.value = '参考价须大于 0';
    return;
  }
  if (
    !Number.isFinite(Number(tolerance.value)) ||
    Number(tolerance.value) < 0 ||
    Number(tolerance.value) > 100
  ) {
    error.value = '价格浮动须为 0 至 100%';
    return;
  }
  void perform('保存中', () =>
    competitorApi.save(props.run, {
      expected_version: version(),
      reference_price: referencePrice.value || null,
      currency: currency.value,
      tolerance: String(Number(tolerance.value) / 100),
      reviews: reviews.value,
    }),
  );
}
const labels: Record<string, string> = {
  unknown: '未知',
  within: '价格段内',
  outside: '超出价格段',
  live: '商品页当前价',
  average_90d: '90 天平均售价',
  average_360d: '360 天平均售价',
  export: '导出售价',
};
const safeUrl = (value: string) => {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : '';
  } catch {
    return '';
  }
};
const hosts: Record<string, string> = {
  US: 'www.amazon.com',
  CA: 'www.amazon.ca',
  UK: 'www.amazon.co.uk',
  DE: 'www.amazon.de',
  FR: 'www.amazon.fr',
  IT: 'www.amazon.it',
  ES: 'www.amazon.es',
  JP: 'www.amazon.co.jp',
};
</script>

<template>
  <section class="competitors-panel" aria-labelledby="competitors-title">
    <p v-if="syncPending" role="status" class="sync-notice">
      后台结果有更新；当前操作或未保存修改结束后将自动同步。也可使用面板的读取按钮重新加载。
    </p>
    <header>
      <div>
        <small>准备 · 第三步</small>
        <h2 id="competitors-title">筛选竞对 ASIN</h2>
        <p>按市场调研、前台重复结果或商机探测器商品数据，核对图片、相关性和价格段。</p>
      </div>
      <div class="header-actions">
        <!-- 将保存入口同时放在标题旁，避免多张商品卡片把底部按钮推到屏幕外。 -->
        <button
          :disabled="!!busy || !coreReady || !workspace || workspace.stale || !kept || saved"
          @click="save"
        >
          {{ saved ? '已保存' : '保存选择' }}
        </button>
        <button :disabled="!!busy || dirty" @click="reload">重新读取</button>
      </div>
    </header>
    <p v-if="!coreReady" class="notice">请先保存第二步核心大词选择，再添加或确认竞对。</p>
    <p v-if="workspace?.stale" class="notice">
      核心大词已变化，此竞对快照已过期。添加当前候选后重新核对。
    </p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="busy" role="status">{{ busy }}…</p>
    <p v-if="dirty" class="notice">
      人工核对有未保存修改。请先保存，或点击“放弃修改并读取”恢复服务端记录。<button
        :disabled="!!busy"
        @click="reload"
      >
        放弃修改并读取
      </button>
    </p>
    <fieldset :disabled="!!busy || !coreReady || dirty">
      <div class="sources">
        <div>
          <h3>方法一 · 市场调研</h3>
          <label
            >竞对 ASIN（最多 100 个）<textarea
              v-model="manual"
              rows="3"
              placeholder="每行一个 ASIN"
            /></label
          ><button @click="discover('manual')">添加人工候选</button>
        </div>
        <div>
          <h3>方法二 · 前台重复 ASIN</h3>
          <p>
            自动搜索全部核心大词，各取第一页前 30
            个自然商品并排除广告；先按跨词重复次数、再按已有四维相关度从高到低排序，不限制前 10 个。
          </p>
          <p class="hint">将搜索 {{ keywords.length }} 个已确认核心大词。</p>
          <button @click="discover('front_search')">搜索全部核心词的自然结果</button>
        </div>
        <div>
          <h3>方法三 · 商机探测器</h3>
          <button :disabled="!keywords.length || marketplace !== 'US'" @click="downloadPoe">
            下载全部核心词并自动筛选
          </button>
          <p class="hint">
            系统会逐个搜索全部核心大词，从购买意图列表中选择与本品用途、结构和规格最相关的一项；进入详情的“商品”页并下载最近 30
            天商品表。先按最近 30 天平均售价筛选本品 ±15% 价格段，再对价格通过的 ASIN
            计算四维相关度；相关度 >40 的商品直接确认为竞对 ASIN。目前支持美国站，下载期间请保持紫鸟打开。
          </p>
          <p class="notice">
            自动筛选完成后可直接进入下一步；若超过反向拓词的 100 个 ASIN 上限，按相关度保留前 100
            个。方法一、方法二仍需人工核对并保存。
          </p>
          <details class="manual-csv">
            <summary>手动上传 CSV（备用）</summary>
            <p>
              卖家后台 → 商品 → 研究商品 → 商机探测器 → 搜索核心大词 → 商品 → 下载
              CSV。也可从搜索词页人工挑选 ASIN。
            </p>
            <label
              >商品 CSV<input type="file" accept=".csv,.tsv,text/csv" @change="readCsv" /></label
            ><label
              >来源名称<input
                v-model="sourceLabel"
                maxlength="200"
                placeholder="例如 cooling blanket 商品页"
            /></label>
            <p class="hint">
              支持 ASIN、Product Name、Average Selling Price (90 days)、Currency、Image URL
              等列，也支持亚马逊原件说明行及 360 天均价列。价格用 23.50，币种用 USD
              等三字母代码。缺失列保留为未知。
            </p>
            <button :disabled="!csvText" @click="discover('poe_csv')">手动导入商品候选</button>
          </details>
          <div v-if="workspace?.poe_downloads?.length" class="download-history">
            <h4>购买意图选择与下载结果</h4>
            <article v-for="file in workspace.poe_downloads" :key="file.id" class="intent-result">
              <strong>{{ file.keyword }}</strong>
              <span :class="file.status === 'failed' ? 'error' : ''">
                {{ file.status === 'completed' ? '已完成' : '失败' }}
              </span>
              <template v-if="file.status === 'completed'">
                <p>所选购买意图：{{ file.intent_name }} · 置信度 {{ file.intent_confidence }}</p>
                <p>选择理由：{{ file.intent_reason }}</p>
                <p>
                  30 天商品 {{ file.rows }} · 价格通过 {{ file.price_qualified_rows }} · 相关度通过
                  {{ file.relevance_qualified_rows ?? '未提供' }}
                </p>
                <a :href="competitorApi.originalUrl(run, file.id)" download>下载原始 CSV</a>
                <small>{{ file.filename }}</small>
              </template>
              <template v-else>
                <p v-if="file.intent_name">已选购买意图：{{ file.intent_name }}</p>
                <p v-if="file.intent_reason">选择理由：{{ file.intent_reason }}</p>
                <p class="error">
                  失败阶段：{{ stageLabels[file.stage] || '未知阶段' }}；{{ file.error }}
                </p>
                <button :disabled="!!busy" @click="retryPoe(file.keyword)">只重试此核心词</button>
              </template>
            </article>
          </div>
        </div>
      </div>
    </fieldset>
    <template v-if="workspace">
      <p v-for="warning in workspace.warnings" :key="warning" class="notice">{{ warning }}</p>
      <fieldset
        :disabled="!!busy || workspace.stale || !coreReady"
        @input="dirty = true"
        @change="dirty = true"
      >
        <div class="prices">
          <label
            >本品参考价<input
              v-model="referencePrice"
              type="number"
              min="0.0001"
              step="0.0001"
              placeholder="待填写" /></label
          ><label
            >币种<select v-model="currency">
              <option v-for="code in ['USD', 'CAD', 'GBP', 'EUR', 'JPY']" :key="code">
                {{ code }}
              </option>
            </select></label
          ><label
            >价格浮动（±%）<input v-model="tolerance" type="number" min="0" max="100" step="1"
          /></label>
        </div>
        <p class="hint">
          价格标签按上次保存的参考价计算。修改后需重新保存。价格未知、币种不同或超段时，保留须填写价格核对说明。
        </p>
        <article v-for="(item, index) in workspace.items" :key="item.asin" class="candidate">
          <img
            v-if="safeUrl(item.image_url)"
            :src="safeUrl(item.image_url)"
            alt="候选商品主图"
            loading="lazy"
            referrerpolicy="no-referrer"
          />
          <div v-else class="no-image">主图待核对</div>
          <div class="candidate-main">
            <a
              :href="`https://${hosts[marketplace] || 'www.amazon.com'}/dp/${item.asin}`"
              target="_blank"
              rel="noopener noreferrer"
              >{{ item.asin }}</a
            >
            <h3>{{ item.title || '商品标题待补充' }}</h3>
            <p>
              {{ item.price ?? '价格未知' }} {{ item.currency ?? '币种未知' }} ·
              {{ labels[item.price_basis] }} · {{ labels[item.price_status] }} · 跨词命中
              {{ item.keyword_hits }} · 四维相关度
              {{ item.relevance_score === null ? '无已有证据' : `${item.relevance_score} 分` }}
            </p>
            <details>
              <summary>查看来源与自然位置</summary>
              <p v-for="(source, sourceIndex) in item.sources" :key="sourceIndex">
                {{ source.label }} {{ source.keyword }}
                <span v-if="source.position">· 自然位置 {{ source.position }}</span>
                {{ source.region }} · {{ source.captured_at }}
                <a
                  v-if="safeUrl(source.url)"
                  :href="safeUrl(source.url)"
                  target="_blank"
                  rel="noopener noreferrer"
                  >来源页</a
                >
              </p>
            </details>
            <button :disabled="dirty" @click.prevent="details(item.asin)">
              读取商品主图与当前价
            </button>
          </div>
          <div v-if="reviews[index]" class="review">
            <label
              >人工判断<select v-model="reviews[index].decision">
                <option value="pending">待核对</option>
                <option value="include">保留为竞对</option>
                <option value="exclude">排除</option>
              </select></label
            ><label
              >相关性 / 排除理由<textarea
                v-model="reviews[index].reason"
                rows="2"
                maxlength="1000"
                placeholder="结合主图、用途、规格说明"
              /></label
            ><label
              >价格核对说明<textarea
                v-model="reviews[index].price_override_reason"
                rows="2"
                maxlength="1000"
                placeholder="价格未知或超段时保留必填"
              />
            </label>
          </div>
        </article>
        <p v-if="!workspace.items.length">
          本次范围内没有候选。请查看采集提示，或通过其他方法添加。
        </p>
        <section v-if="workspace.items.length" class="bulk-review" aria-label="批量填写说明">
          <h3>批量填写说明</h3>
          <p>
            应用到已保留或排除的
            {{ decidedCount }} 个候选，仅填充空白项，已有说明保留。待核对的候选不受影响。
          </p>
          <div class="bulk-fields">
            <label
              >批量核对理由<textarea
                v-model="bulkReason"
                rows="2"
                maxlength="1000"
                placeholder="填写适用于这些商品的实际核对理由"
              />
            </label>
            <label
              >批量价格说明<textarea
                v-model="bulkPriceNote"
                rows="2"
                maxlength="1000"
                placeholder="仅补充到保留候选的空白价格说明中"
              />
            </label>
          </div>
          <button
            type="button"
            :disabled="!decidedCount || (!bulkReason.trim() && !bulkPriceNote.trim())"
            @click="fillBlankNotes"
          >
            填充空白说明
          </button>
          <p v-if="bulkFeedback" role="status">{{ bulkFeedback }}</p>
        </section>
        <footer>
          <div class="save-feedback" aria-live="polite">
            <!-- 底部保存必须在原位置反馈，不能让用户到九张卡片上方找错误。 -->
            <p v-if="error" class="error">保存未完成：{{ error }}</p>
            <p v-else-if="busy === '保存中'">正在保存竞对选择，请稍候…</p>
            <p v-else-if="saved">竞对选择已保存，已保留 {{ kept }} 个竞对。</p>
            <p v-else-if="!kept">请至少将一个候选设为“保留为竞对”。</p>
            <p v-else>保存前请填写保留或排除理由；价格未知或超段时还需填写价格核对说明。</p>
            <span
              >候选 {{ workspace.items.length }} · 保留 {{ kept }} / 100 · 版本
              {{ workspace.version }}</span
            >
          </div>
          <button :disabled="!!busy || !kept || saved" @click="save">
            {{ busy === '保存中' ? '保存中…' : saved ? '竞对选择已保存' : '保存竞对选择' }}
          </button>
        </footer>
      </fieldset>
    </template>
  </section>
</template>

<style scoped>
.bulk-review {
  padding: 16px;
  margin: 16px 0;
  border: 1px solid #dce6e2;
  border-radius: 12px;
  background: #f5faf8;
}
.bulk-fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(min(100%, 240px), 1fr));
  gap: 12px;
  margin-bottom: 12px;
}

.header-actions {
  display: flex;
  gap: 8px;
  align-items: center;
}
.competitors-panel {
  margin-top: 24px;
  padding: 24px;
  background: white;
  border: 1px solid #dce6e2;
  border-radius: 18px;
  color: #233d35;
}
header,
footer,
.prices {
  display: flex;
  gap: 16px;
  justify-content: space-between;
  align-items: center;
}
h2 {
  margin: 6px 0;
  font-size: 22px;
}
h3 {
  margin: 8px 0;
  font-size: 15px;
}
p {
  font-size: 13px;
  line-height: 1.65;
}
small,
a {
  color: #087d63;
}
fieldset {
  border: 0;
  margin: 16px 0 0;
  padding: 0;
  min-width: 0;
}
fieldset:disabled {
  opacity: 0.65;
}
.sources {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 16px;
}
.sources > div {
  padding: 16px;
  background: #f6f9f7;
  border-radius: 12px;
}
label {
  display: block;
  font-size: 12px;
  margin: 8px 0;
}
input:not([type='checkbox']),
textarea,
select {
  display: block;
  box-sizing: border-box;
  width: 100%;
  margin-top: 6px;
  padding: 9px;
  border: 1px solid #ccdcd4;
  border-radius: 7px;
  font: inherit;
  background: white;
}
textarea {
  resize: vertical;
}
.check {
  display: flex;
  gap: 8px;
  align-items: center;
}
button {
  padding: 9px 14px;
  border: 1px solid #afcdc0;
  background: #edf6f1;
  color: #076e56;
  border-radius: 8px;
  cursor: pointer;
  font: inherit;
  font-size: 12px;
}
button:disabled {
  opacity: 0.5;
  cursor: default;
}
.notice {
  padding: 10px;
  background: #fff7df;
  border-radius: 8px;
}
.error {
  color: #b52c2c;
}
.hint {
  color: #6b7b73;
  font-size: 12px;
}
.prices {
  justify-content: flex-start;
}
.prices label {
  width: 160px;
}
.candidate {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr) minmax(200px, 30%);
  gap: 18px;
  border-top: 1px solid #e2eae5;
  padding: 18px 0;
}
.candidate img,
.no-image {
  width: 100px;
  height: 110px;
  object-fit: contain;
}
.no-image {
  display: flex;
  align-items: center;
  justify-content: center;
  background: #f1f5f2;
  font-size: 12px;
}
.candidate-main {
  overflow-wrap: anywhere;
}
summary {
  font-size: 12px;
  cursor: pointer;
}
footer {
  padding-top: 16px;
  font-size: 13px;
}
@media (max-width: 1000px) {
  .sources {
    grid-template-columns: 1fr;
  }
  .candidate {
    grid-template-columns: 80px minmax(0, 1fr);
  }
  .review {
    grid-column: 1/-1;
  }
}
@media (max-width: 600px) {
  header,
  .prices,
  footer {
    align-items: flex-start;
    flex-wrap: wrap;
  }
  .competitors-panel {
    padding: 16px;
  }
}
.manual-csv {
  margin-top: 12px;
}
.download-history p {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 10px;
}
.intent-result {
  margin-top: 10px;
  padding: 10px;
  border: 1px solid #dce6e2;
  border-radius: 8px;
  background: white;
}
.intent-result p {
  margin: 4px 0;
}
.download-history small {
  flex-basis: 100%;
  overflow-wrap: anywhere;
}
</style>
