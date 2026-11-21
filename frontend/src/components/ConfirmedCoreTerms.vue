<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { coreApi } from '../api/coreTerms';
import type { InputRun } from '../api/images';

/** 只读摘要：第三步实际使用的已确认核心大词，取数与 CompetitorsPanel 同源。 */
const props = defineProps<{ run: InputRun }>();
const emit = defineEmits<{ goto: [step: number] }>();

const LIMIT = 6;
const terms = ref<Array<{ text: string; translation: string }>>([]);
const stale = ref(false);
const loading = ref(true);
const error = ref('');
const expanded = ref(false);
const visible = computed(() => (expanded.value ? terms.value : terms.value.slice(0, LIMIT)));
const hidden = computed(() => Math.max(0, terms.value.length - LIMIT));
let epoch = 0;
let alive = true;
onBeforeUnmount(() => {
  alive = false;
  epoch++;
});

async function load() {
  const current = ++epoch;
  loading.value = true;
  error.value = '';
  try {
    const selection = await coreApi.selection(props.run);
    if (!alive || current !== epoch) return;
    stale.value = selection?.stale === true;
    // 现有确认记录的中文翻译多为空，退化为 ABA 排名，避免只剩英文词。
    terms.value = (selection?.items ?? []).map((item) => {
      const translation = (item.translation || '').trim();
      const rank = item.search_frequency_rank;
      return {
        text: item.keyword,
        translation: translation || (rank > 0 ? `排名 ${rank.toLocaleString('en-US')}` : ''),
      };
    });
  } catch (e) {
    if (alive && current === epoch) {
      error.value = e instanceof Error ? e.message : '读取已确认核心大词失败';
    }
  } finally {
    if (alive && current === epoch) loading.value = false;
  }
}

watch(
  () => [props.run.project_id, props.run.id],
  () => {
    expanded.value = false;
    void load();
  },
  { immediate: true },
);
</script>

<template>
  <section class="confirmed-core-terms" aria-label="本步用到的已确认核心大词">
    <div class="terms-head">
      <span>本次筛选用到的核心大词</span>
      <small v-if="!loading && !error && terms.length">
        来自第二步已确认 · {{ terms.length }} 个
      </small>
    </div>
    <p v-if="loading" class="terms-note">正在读取第二步确认结果…</p>
    <p v-else-if="error" class="terms-error" role="alert">{{ error }}</p>
    <template v-else-if="!terms.length">
      <p class="terms-warn">第二步尚未保存核心大词，本步无法筛选竞对。</p>
      <button type="button" class="terms-link" @click="emit('goto', 1)">去第二步确认 →</button>
    </template>
    <template v-else>
      <ul class="terms-list">
        <li v-for="item in visible" :key="item.text">
          <strong>{{ item.text }}</strong>
          <small v-if="item.translation">{{ item.translation }}</small>
        </li>
      </ul>
      <button v-if="hidden > 0" type="button" class="terms-link" @click="expanded = !expanded">
        {{ expanded ? '收起' : `展开全部（共 ${terms.length} 个）` }}
      </button>
      <p v-if="stale" class="terms-warn">核心大词已变化，这里是保存时的快照。</p>
    </template>
  </section>
</template>

<style scoped>
.confirmed-core-terms {
  margin: 13px 0 0;
  padding: 10px 12px;
  border: 1px solid #dde8e1;
  border-radius: 10px;
  background: #fbfdfc;
}
.terms-head {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}
.terms-head span {
  color: #1f2d28;
  font-size: 12px;
  font-weight: 700;
}
.terms-head small {
  color: #73867c;
  font-size: 11px;
}
.terms-list {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.terms-list li {
  display: inline-flex;
  align-items: baseline;
  gap: 4px;
  max-width: 100%;
  padding: 3px 10px;
  border: 1px solid #dde8e1;
  border-radius: 999px;
  background: white;
}
.terms-list strong {
  color: #1f2d28;
  font-size: 12px;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.terms-list small {
  color: #73867c;
  font-size: 11px;
}
.terms-note,
.terms-warn,
.terms-error {
  margin: 0;
  font-size: 12px;
  line-height: 1.7;
}
.terms-note {
  color: #73867c;
}
.terms-warn {
  color: #8a5a00;
  margin-top: 8px;
}
.terms-error {
  color: #b42318;
}
.terms-link {
  margin-top: 8px;
  padding: 0;
  border: 0;
  background: none;
  color: #247864;
  font-size: 12px;
  cursor: pointer;
}
</style>
