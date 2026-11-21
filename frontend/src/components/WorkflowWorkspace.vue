<script setup lang="ts">
import { computed } from 'vue';
import { Check, ChevronRight, LoaderCircle } from '@lucide/vue';
import { steps, prerequisites, type StepStatus } from '../workflowGuide';
import type { WorkflowView } from '../api/workflow';
import { buildTaskCenter } from '../workflowTaskCenter';
const props = defineProps<{
  current: number;
  statuses: StepStatus[];
  facts: StepStatus[];
  instruction: string;
  runReady: boolean;
  workflow?: WorkflowView | null;
  error?: string;
}>();
const selected = defineModel<number>({ required: true });
const emit = defineEmits<{ navigate: [step: number] }>();
const count = computed(() => props.facts.filter((s) => s === 'done').length);
const blocked = computed(() =>
  prerequisites(selected.value).filter((index) => props.facts[index] !== 'done'),
);
const taskCenter = computed(() =>
  buildTaskCenter({
    current: props.current,
    selected: selected.value,
    facts: props.facts,
    steps,
    instruction: props.instruction,
    error: props.error,
    workflow: props.workflow ?? null,
  }),
);
const groups = [
  { title: '01　准备与确认', indexes: [0, 1, 2] },
  { title: '02　两路数据采集 · 可并行', indexes: [3, 4] },
  { title: '03　汇总、审核与发布', indexes: [5, 6, 7] },
];
function go(index: number) {
  selected.value = index;
  emit('navigate', index);
}
function label(index: number) {
  if (props.facts[index] === 'done') return '已完成 · 可回看';
  if (props.statuses[index] === 'running') return '系统自动执行中';
  if (props.statuses[index] === 'review') return '等待你处理';
  if (index === props.current) return props.error ? '需要处理' : '当前待办';
  if (prerequisites(index).some((i) => props.facts[i] !== 'done')) return '等待前置步骤';
  return '可处理';
}
</script>

<template>
  <section
    class="workspace-guide"
    :data-mode="taskCenter.mode"
    aria-label="当前待办"
  >
    <div>
      <span class="guide-kicker">{{ taskCenter.kicker }}</span>
      <h2>{{ taskCenter.title }}</h2>
      <p :class="{ 'guide-error': taskCenter.mode === 'error' }">{{ taskCenter.description }}</p>
    </div>
    <button class="guide-primary" type="button" @click="go(taskCenter.primaryStep)">
      {{ taskCenter.primaryLabel }}<ChevronRight :size="16" />
    </button>
  </section>
  <slot name="controls" />
  <div class="step-workspace">
    <nav class="step-navigation" aria-label="八步操作导航">
      <div class="step-progress">
        <strong>研究流程</strong><span>{{ count }} / 8 已完成</span>
      </div>
      <div v-for="group in groups" :key="group.title">
        <h3>{{ group.title }}</h3>
        <button
          v-for="index in group.indexes"
          :key="index"
          type="button"
          :class="[
            'step-nav-item',
            { selected: selected === index, complete: facts[index] === 'done' },
          ]"
          :aria-current="selected === index ? 'step' : undefined"
          :data-step="index"
          @click="go(index)"
        >
          <span class="step-nav-number"
            ><Check v-if="facts[index] === 'done'" :size="14" />
            <LoaderCircle v-else-if="statuses[index] === 'running'" :size="14" class="spin" />
            <span v-else>{{ index + 1 }}</span></span
          >
          <span
            ><strong>{{ steps[index]?.title }}</strong
            ><small>{{ label(index) }}</small></span
          >
        </button>
      </div>
      <p class="navigation-note">查看步骤只切换页面，不会提交采集或审核。</p>
    </nav>
    <section
      id="step-operation"
      class="step-operation"
      :aria-label="`第 ${selected + 1} 步操作`"
      tabindex="-1"
    >
      <div v-if="selected === current" class="task-center-sections">
        <section class="task-card task-actions" aria-labelledby="task-actions-title">
          <div class="task-card-head">
            <span class="task-label">你现在要做</span>
            <small v-if="taskCenter.mode === 'human'">完成面板操作后点击保存</small>
            <small v-else-if="taskCenter.mode === 'error'">请先处理错误后重试</small>
            <small v-else-if="taskCenter.mode === 'automatic'">无需人工推进，可查看当前状态</small>
            <small v-else>流程已完成，可查看发布结果</small>
          </div>
          <h3 id="task-actions-title">{{ taskCenter.title }}</h3>
          <ol>
            <li v-for="(item, index) in taskCenter.actions" :key="item">
              <span>{{ index + 1 }}</span><strong>{{ item }}</strong>
            </li>
          </ol>
          <slot name="task-input" />
          <p v-if="taskCenter.mode === 'human'" class="task-save-note">
            保存后系统自动继续；查看步骤或日志不会推进流程。
          </p>
        </section>
        <section class="task-card task-output" aria-labelledby="task-output-title">
          <div class="task-card-head">
            <span class="task-label">系统刚刚完成</span>
          </div>
          <h3 id="task-output-title">{{ taskCenter.outputTitle }}</h3>
          <p v-if="taskCenter.outputMessage">{{ taskCenter.outputMessage }}</p>
          <dl v-if="taskCenter.outputs.length">
            <template v-for="([key, value]) in taskCenter.outputs" :key="key">
              <dt>{{ key }}</dt><dd>{{ value }}</dd>
            </template>
          </dl>
          <p v-else class="task-empty">{{ taskCenter.outputEmpty }}</p>
        </section>
        <section class="task-next" aria-label="下一步自动衔接">
          <span class="task-label">下一步自动衔接</span>
          <strong>{{ taskCenter.nextTitle }}</strong>
          <p>{{ taskCenter.nextDescription }}</p>
        </section>
      </div>
      <div v-else class="step-context">
        <span class="guide-kicker">STEP {{ String(selected + 1).padStart(2, '0') }} / 08</span>
        <h2>{{ steps[selected]?.title }}</h2>
        <p>{{ steps[selected]?.note }}</p>
        <ol class="operation-order">
          <li v-for="(item, index) in steps[selected]?.flow" :key="item">
            <span>{{ index + 1 }}</span
            >{{ item }}
          </li>
        </ol>
        <slot name="step-input" />
        <div v-if="blocked.length || (!runReady && selected > 0)" class="step-blocked">
          <strong
            >本步需要先完成：{{
              blocked.map((i) => steps[i]?.title).join('、') || '保存商品输入'
            }}</strong
          >
          <p>已有结果仍可回看。新操作以面板提示及服务端校验为准。</p>
          <button type="button" @click="go(blocked[0] ?? 0)">去处理前置步骤 →</button>
        </div>
        <p v-if="selected === 3 || selected === 4" class="join-note">
          ABA 按所选报告周，卖家精灵按最近 30 天采集。两路齐备后汇入清洗，仅合并关键词文本。
          <button type="button" @click="go(selected === 3 ? 4 : 3)">
            查看{{ selected === 3 ? '卖家精灵' : 'ABA' }}路线 →
          </button>
        </p>
      </div>
      <slot />
      <footer class="step-footer">
        <button type="button" :disabled="selected === 0" @click="go(selected - 1)">← 上一步</button>
        <span>{{
          facts[selected] === 'done'
            ? '本步已保存完成，可以查看下一步。'
            : '请在本步面板完成操作，系统按实际保存结果更新进度。'
        }}</span>
        <button
          v-if="facts[selected] === 'done' && selected < 7"
          class="guide-primary"
          type="button"
          @click="go(selected + 1)"
        >
          查看下一步 →
        </button>
        <button v-else-if="selected !== current" type="button" @click="go(current)">
          回到当前待办 →
        </button>
      </footer>
    </section>
  </div>
</template>

<style scoped>
.workspace-guide {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 20px;
  padding: 20px 24px;
  border: 1px solid #cde2db;
  background: #edf6f2;
  border-radius: 12px;
  margin-bottom: 22px;
}
.workspace-guide[data-mode='human'] {
  border-left: 4px solid #c58b2b;
  background: #fff8e9;
}
.workspace-guide[data-mode='automatic'] {
  border-left: 4px solid #2f6fae;
  background: #eef5fc;
}
.workspace-guide[data-mode='error'] {
  border-left: 4px solid #b2413c;
  background: #fff1ef;
}
.guide-kicker {
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 1px;
  color: #288473;
}
.workspace-guide h2 {
  font-size: 19px;
  margin: 8px 0;
}
.workspace-guide p,
.step-context p {
  font-size: 13px;
  line-height: 1.8;
  color: #657b74;
  margin: 8px 0;
}
.guide-primary {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 7px;
  border: 1px solid #207b68 !important;
  border-radius: 7px;
  background: #207b68 !important;
  color: white !important;
  padding: 10px 15px;
  flex-shrink: 0;
}
.step-workspace {
  display: grid;
  grid-template-columns: 235px minmax(0, 1fr);
  gap: 22px;
  align-items: start;
}
.step-navigation {
  background: white;
  border: 1px solid #e1e8e4;
  border-radius: 12px;
  padding: 18px 10px;
  position: sticky;
  top: 78px;
}
.step-progress {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 0 10px 12px;
  font-size: 13px;
}
.step-progress span {
  font-size: 11px;
  color: #7c8d85;
}
.step-navigation h3 {
  font-size: 11px;
  font-weight: 500;
  letter-spacing: 1px;
  color: #809087;
  margin: 19px 10px 9px;
}
.step-nav-item {
  width: 100%;
  border: 0;
  background: white;
  display: flex;
  gap: 10px;
  text-align: left;
  align-items: center;
  padding: 12px 10px;
  border-radius: 7px;
  color: #546a60;
  margin: 3px 0;
}
.step-nav-item.selected {
  background: #edf6f2;
  box-shadow: inset 3px 0 #278673;
  color: #176e5c;
}
.step-nav-number {
  width: 25px;
  height: 25px;
  display: flex;
  justify-content: center;
  align-items: center;
  border: 1px solid #d8e2dc;
  border-radius: 50%;
  font-size: 11px;
  flex-shrink: 0;
}
.complete .step-nav-number {
  background: #e4f2e9;
  border: 0;
  color: #287651;
}
.step-nav-item strong {
  display: block;
  font-size: 12px;
  margin-bottom: 5px;
}
.step-nav-item small {
  font-size: 11px;
  color: #83938b;
}
.navigation-note {
  font-size: 11px;
  color: #84958b;
  line-height: 1.7;
  padding: 14px 10px 0;
  border-top: 1px solid #edf1ee;
}
.step-operation {
  min-width: 0;
  background: white;
  border: 1px solid #e1e8e4;
  border-radius: 12px;
  overflow: clip;
  outline: none;
}
.step-context {
  padding: 24px 26px 18px;
  border-bottom: 1px solid #edf1ee;
}
.task-center-sections {
  display: grid;
  grid-template-columns: minmax(0, 1.35fr) minmax(240px, 0.8fr);
  gap: 16px;
  padding: 22px 24px;
  border-bottom: 1px solid #edf1ee;
  background: #f8fbf9;
}
.task-card {
  min-width: 0;
  padding: 18px;
  border: 1px solid #dde8e1;
  border-radius: 10px;
  background: white;
}
.task-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;
}
.task-card-head small,
.task-card p,
.task-next p {
  color: #73867c;
  font-size: 12px;
  line-height: 1.7;
}
.task-label {
  color: #247864;
  font-size: 11px;
  font-weight: 700;
  letter-spacing: 0.8px;
}
.task-card h3 {
  margin: 0 0 12px;
  font-size: 16px;
}
.task-actions ol {
  display: grid;
  gap: 9px;
  margin: 0;
  padding: 0;
  list-style: none;
}
.task-actions li {
  display: grid;
  grid-template-columns: 24px minmax(0, 1fr);
  gap: 9px;
  align-items: center;
  padding: 9px 0;
  border-bottom: 1px solid #edf1ee;
  color: #43594e;
  font-size: 13px;
}
.task-actions li:last-child {
  border-bottom: 0;
}
.task-actions li span {
  display: grid;
  width: 23px;
  height: 23px;
  place-items: center;
  border-radius: 50%;
  background: #eaf3f9;
  color: #2f6fae;
  font-size: 11px;
}
.task-actions li strong {
  font-weight: 500;
}
.task-save-note {
  margin: 13px 0 0;
  padding: 10px 12px;
  background: #edf6f2;
}
.task-output dl {
  display: grid;
  grid-template-columns: minmax(90px, auto) minmax(0, 1fr);
  gap: 8px 12px;
  margin: 14px 0 0;
  font-size: 12px;
}
.task-output dt {
  color: #778a80;
}
.task-output dd {
  min-width: 0;
  margin: 0;
  color: #334b40;
  font-weight: 600;
  overflow-wrap: anywhere;
}
.task-output .task-empty {
  margin: 12px 0 0;
}
.task-next {
  grid-column: 1 / -1;
  display: grid;
  grid-template-columns: auto minmax(150px, auto) minmax(0, 1fr);
  gap: 12px;
  align-items: center;
  padding: 13px 15px;
  border: 1px dashed #cfded5;
  border-radius: 9px;
  background: white;
}
.task-next p {
  margin: 0;
}
.step-context h2 {
  font-size: 22px;
  margin: 10px 0;
}
.operation-order {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  list-style: none;
  padding: 0;
  margin: 18px 0 2px;
}
.operation-order li {
  font-size: 12px;
  color: #577466;
  background: #f2f7f4;
  border-radius: 6px;
  padding: 8px 10px;
  display: flex;
  gap: 8px;
  align-items: center;
}
.operation-order li span {
  color: #27806a;
  font-weight: 700;
}
.step-blocked {
  padding: 14px;
  background: #fffbef;
  border-left: 3px solid #d6b76d;
  margin-top: 18px;
  font-size: 12px;
  color: #89733c;
}
.step-blocked button,
.join-note button {
  border: 0;
  background: transparent;
  color: #207d69;
  font-size: 12px;
  padding: 4px 0;
}
.step-context .join-note {
  background: #f6f9f7;
  padding: 12px;
  font-size: 12px;
  margin-top: 16px;
}
.join-note button {
  display: block;
}
.step-footer {
  padding: 18px 24px;
  border-top: 1px solid #e6ede8;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  background: #fafcfb;
}
.step-footer span {
  font-size: 12px;
  color: #7f9187;
  line-height: 1.7;
}
.step-footer button {
  border: 1px solid #d8e4dc;
  border-radius: 6px;
  background: white;
  color: #536d5e;
  padding: 9px 13px;
  flex-shrink: 0;
}
.step-footer button:disabled {
  opacity: 0.4;
}
.workspace-guide .guide-error {
  color: #b2413c;
}
button {
  cursor: pointer;
}
button:disabled {
  cursor: default;
}
@media (max-width: 850px) {
  .step-workspace {
    grid-template-columns: 1fr;
  }
  .step-navigation {
    position: static;
    display: flex;
    overflow: auto;
    padding: 10px;
    gap: 10px;
  }
  .step-progress,
  .navigation-note {
    display: none;
  }
  .step-navigation > div {
    display: flex;
    gap: 6px;
  }
  .step-navigation h3 {
    display: none;
  }
  .step-nav-item {
    min-width: 172px;
    margin: 0;
  }
  .workspace-guide {
    padding: 17px;
    align-items: flex-start;
    flex-direction: column;
  }
  .step-context {
    padding: 20px;
  }
  .task-center-sections {
    grid-template-columns: 1fr;
    padding: 16px;
  }
  .task-next {
    grid-column: auto;
    grid-template-columns: 1fr;
  }
  .step-footer {
    flex-wrap: wrap;
    padding: 18px;
  }
  .step-footer span {
    order: 3;
    width: 100%;
  }
}
</style>
