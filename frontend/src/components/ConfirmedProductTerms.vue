<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { ImageInputClient, type InputRun } from '../api/images';

/** 只读摘要：第二步拓展实际使用的商品词，口径与后端 AbaService._context 一致。 */
const props = defineProps<{ run: InputRun }>();
const emit = defineEmits<{ goto: [step: number] }>();

const client = new ImageInputClient();
const LIMIT = 6;
const terms = ref<Array<{ text: string; translation: string }>>([]);
const legacyRule = ref(false);
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
    const [selection, candidates] = await Promise.all([
      client.getRootSelection(props.run),
      client.getRoots(props.run),
    ]);
    if (!alive || current !== epoch) return;
    legacyRule.value = candidates?.rule_version === 'legacy-root-v1';
    const confirmed = new Set(selection?.candidate_ids ?? []);
    terms.value = (candidates?.candidates ?? [])
      .filter((item) => confirmed.has(item.id))
      .map((item) => ({ text: item.text, translation: item.translation }));
  } catch (e) {
    if (alive && current === epoch) {
      error.value = e instanceof Error ? e.message : '读取已确认商品词失败';
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
  <section class="confirmed-terms" aria-label="本次拓展用到的商品词">
    <div class="terms-head">
      <span>本次拓展用到的商品词</span>
      <small v-if="!loading && !error && terms.length">
        来自第一步已确认 · {{ terms.length }} 个
      </small>
    </div>
    <p v-if="loading" class="terms-note">正在读取第一步确认结果…</p>
    <p v-else-if="error" class="terms-error" role="alert">{{ error }}</p>
    <template v-else-if="legacyRule">
      <p class="terms-warn">当前确认记录来自旧规则（词根），请回第一步按商品词规则重新确认。</p>
      <button type="button" class="terms-link" @click="emit('goto', 0)">去第一步重新确认 →</button>
    </template>
    <template v-else-if="!terms.length">
      <p class="terms-warn">第一步尚未确认商品词，本步无法拓展。</p>
      <button type="button" class="terms-link" @click="emit('goto', 0)">去第一步确认 →</button>
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
    </template>
  </section>
</template>

<style scoped>
.confirmed-terms {
  margin-top: 13px;
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
