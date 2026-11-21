<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Search, RefreshCw, X, FolderOpen, Download, ExternalLink } from '@lucide/vue';
import {
  readResearchRecords,
  researchLink,
  researchExport,
  researchStatuses,
  changeResearchDeleted,
  type ResearchRecord,
  type ResearchPage,
} from '../api/researchRecords';

defineProps<{ currentRun?: string }>();
const emit = defineEmits<{ close: [] }>();
const dialog = ref<HTMLDialogElement>();
const query = ref('');
const status = ref('');
const deleted = ref(false);
const changing = ref(false);
const pendingDelete = ref<ResearchRecord | null>(null);

async function changeRecord(row: ResearchRecord, remove: boolean) {
  if (changing.value) return;
  changing.value = true;
  error.value = '';
  try {
    await changeResearchDeleted(row, remove);
    pendingDelete.value = null;
    await load(0);
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '记录操作失败';
  } finally {
    changing.value = false;
  }
}
const page = ref<ResearchPage>({ total: 0, offset: 0, limit: 20, items: [] });
const busy = ref(false);
const error = ref('');
let revision = 0;

async function load(offset = 0) {
  // 快速搜索/翻页可能产生并发读取，只让最后一次请求更新列表和忙碌状态。
  const current = ++revision;
  busy.value = true;
  error.value = '';
  try {
    const result = await readResearchRecords(
      query.value.trim(),
      status.value,
      offset,
      deleted.value,
    );
    if (current === revision) page.value = result;
  } catch (cause) {
    if (current === revision)
      error.value = cause instanceof Error ? cause.message : '研究记录读取失败';
  } finally {
    if (current === revision) busy.value = false;
  }
}

function date(value: string) {
  return new Date(value).toLocaleString('zh-CN', { hour12: false });
}
onMounted(() => {
  dialog.value?.showModal();
  void load();
});
</script>

<template>
  <dialog
    ref="dialog"
    class="research-dialog"
    aria-labelledby="research-title"
    @close="emit('close')"
  >
    <header class="research-heading">
      <div>
        <span class="research-eyebrow">RESEARCH HISTORY</span>
        <h2 id="research-title"><FolderOpen :size="22" />研究记录</h2>
        <p>每轮研究单独留档；打开记录只读取结果，继续运行请进入工作台操作。</p>
      </div>
      <button type="button" class="icon-button" aria-label="关闭研究记录" @click="dialog?.close()">
        <X :size="20" />
      </button>
    </header>
    <form class="research-toolbar" @submit.prevent="load()">
      <a class="research-new" href="/?new=1">＋ 新建研究</a>
      <select
        v-model="deleted"
        aria-label="记录范围"
        @change="
          pendingDelete = null;
          load();
        "
      >
        <option :value="false">研究记录</option>
        <option :value="true">已删除（可恢复）</option>
      </select>
      <label class="research-search"
        ><Search :size="17" /><input
          v-model="query"
          aria-label="搜索 ASIN 或商品名称"
          placeholder="搜索 ASIN 或商品名称"
          maxlength="200"
      /></label>
      <select v-model="status" aria-label="筛选研究状态" @change="load()">
        <option value="">全部状态</option>
        <option v-for="(label, key) in researchStatuses" :key="key" :value="key">
          {{ label }}
        </option>
      </select>
      <button type="submit" class="research-primary" :disabled="busy">搜索</button>
      <button type="button" class="research-refresh" :disabled="busy" @click="load(page.offset)">
        <RefreshCw :size="15" />刷新
      </button>
    </form>
    <div v-if="pendingDelete" class="research-error">
      确认将“{{
        pendingDelete.asin || pendingDelete.product_name || '图片研究'
      }}”移入已删除？数据保留，可随时恢复。
      <button :disabled="changing" @click="changeRecord(pendingDelete, true)">确认删除</button>
      <button :disabled="changing" @click="pendingDelete = null">取消</button>
    </div>
    <p class="research-count" aria-live="polite">
      {{ busy ? '正在读取记录…' : `共 ${page.total} 轮研究 · 按创建时间从新到旧排列` }}
    </p>
    <div v-if="error" class="research-error" role="alert">
      {{ error }}<button type="button" @click="load(page.offset)">重试</button>
    </div>
    <div v-else class="research-table-wrap" :aria-busy="busy">
      <table class="research-table">
        <thead>
          <tr>
            <th>商品 / ASIN</th>
            <th>研究时间 / 报告周</th>
            <th>运行状态</th>
            <th>关键词库</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="row in page.items"
            :key="row.id"
            :class="{ 'research-current': row.id === currentRun }"
          >
            <td>
              <strong>{{ row.asin || '图片研究（未提供 ASIN）' }}</strong
              ><span v-if="row.id === currentRun" class="research-current-tag">当前研究</span>
              <p class="research-product">{{ row.product_name || '商品尚未识别' }}</p>
              <small>{{ row.marketplace }} · {{ row.project_name }}</small>
            </td>
            <td>
              <span>{{ date(row.created_at) }}</span>
              <p>ABA 周结束日：{{ row.report_week || '尚未选择' }}</p>
              <small>研究编号 {{ row.id.slice(0, 8) }}…</small>
            </td>
            <td>
              <span class="research-status" :class="`status-${row.status}`">{{
                researchStatuses[row.status] || '状态待确认'
              }}</span>
              <p v-if="row.current_step">{{ row.current_step }}</p>
              <p v-if="row.error" class="research-row-error">{{ row.error }}</p>
            </td>
            <td v-if="row.library">
              <strong>{{ row.library.ranked }} 个有排名词</strong>
              <p>
                共 {{ row.library.total }} 词 ·
                {{
                  row.library.publication_version
                    ? `已发布 v${row.library.publication_version}`
                    : '草稿'
                }}
              </p>
              <small>下载仅包含有 ABA 周排名的词</small>
            </td>
            <td v-else><span class="research-muted">尚未生成</span></td>
            <td>
              <div class="research-row-actions">
                <button
                  v-if="deleted"
                  :disabled="busy || changing"
                  @click="changeRecord(row, false)"
                >
                  恢复记录
                </button>
                <button
                  v-else
                  :disabled="
                    busy ||
                    changing ||
                    ['queued', 'running', 'waiting_external'].includes(row.status)
                  "
                  :title="
                    ['queued', 'running', 'waiting_external'].includes(row.status)
                      ? '请先终止任务再删除'
                      : '移入已删除，可恢复'
                  "
                  @click="pendingDelete = row"
                >
                  删除
                </button>
                <a :href="researchLink(row)" target="_blank" rel="noopener"
                  ><ExternalLink :size="14" />{{
                    row.status === 'completed' ? '查看结果' : '打开 / 继续处理'
                  }}</a
                ><a v-if="row.library" :href="researchExport(row)" download
                  ><Download :size="14" />下载 Excel</a
                >
              </div>
            </td>
          </tr>
          <tr v-if="!busy && !page.items.length">
            <td colspan="5" class="research-empty">
              {{
                query || status
                  ? '没有匹配的研究，请调整搜索或状态筛选。'
                  : '还没有研究记录。保存商品后，这里会自动出现记录。'
              }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <footer class="research-pagination">
      <span>{{
        page.total
          ? `${page.offset + 1}–${Math.min(page.offset + page.items.length, page.total)} / ${page.total}`
          : '0 条记录'
      }}</span>
      <div>
        <button
          :disabled="busy || page.offset === 0"
          @click="load(Math.max(0, page.offset - page.limit))"
        >
          上一页</button
        ><button
          :disabled="busy || page.offset + page.limit >= page.total"
          @click="load(page.offset + page.limit)"
        >
          下一页
        </button>
      </div>
    </footer>
  </dialog>
</template>

<style scoped>
.research-dialog {
  width: min(1240px, calc(100vw - 40px));
  max-height: 88vh;
  border: 1px solid #dce6e1;
  border-radius: 18px;
  padding: 0;
  color: #304640;
  box-shadow: 0 24px 90px #122d3333;
}
.research-dialog::backdrop {
  background: #17372f66;
  backdrop-filter: blur(3px);
}
.research-heading {
  display: flex;
  justify-content: space-between;
  gap: 24px;
  padding: 26px 28px 20px;
  border-bottom: 1px solid #e6ece8;
}
.research-heading h2 {
  display: flex;
  align-items: center;
  gap: 9px;
  margin: 7px 0 9px;
  font-size: 24px;
}
.research-heading p,
.research-count,
.research-pagination {
  font-size: 12px;
  color: #73827c;
}
.research-heading p {
  margin: 0;
  line-height: 1.7;
}
.research-eyebrow {
  letter-spacing: 2px;
  font-size: 10px;
  color: #47816d;
}
.research-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  padding: 20px 28px 0;
}
.research-search {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 220px;
  border: 1px solid #dce5df;
  border-radius: 8px;
  padding: 0 12px;
}
.research-search input {
  width: 100%;
  border: 0;
  padding: 10px 0;
  outline: none;
  background: transparent;
}
.research-search:focus-within {
  outline: 2px solid #458971;
}
.research-toolbar select,
.research-toolbar button,
.research-pagination button {
  padding: 9px 13px;
  border: 1px solid #dce5df;
  border-radius: 8px;
  background: #fff;
  color: #315c4e;
}
.research-toolbar .research-primary {
  background: #22775e;
  color: white;
  border-color: #22775e;
}
.research-refresh {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}
.research-count {
  margin: 14px 28px;
}
.research-table-wrap {
  overflow: auto;
  max-height: 52vh;
  padding: 0 28px;
}
.research-table {
  width: 100%;
  min-width: 920px;
  border-collapse: collapse;
  text-align: left;
  font-size: 12px;
}
.research-table th {
  position: sticky;
  top: 0;
  background: #f3f7f4;
  color: #6f7e77;
  padding: 12px 14px;
  font-weight: 500;
  z-index: 1;
}
.research-table td {
  padding: 17px 14px;
  border-bottom: 1px solid #e8eee9;
  vertical-align: top;
}
.research-table th:first-child {
  width: 27%;
}
.research-table th:last-child {
  width: 145px;
}
.research-table p {
  margin: 7px 0;
  line-height: 1.6;
}
.research-table strong {
  font-size: 13px;
}
.research-table small,
.research-muted {
  color: #7d8c84;
  font-size: 11px;
}
.research-product {
  max-width: 300px;
}
.research-current {
  background: #f5faf7;
}
.research-current-tag {
  display: inline-block;
  margin-left: 7px;
  font-size: 10px;
  color: #22775e;
}
.research-status {
  display: inline-block;
  padding: 4px 8px;
  background: #edf2ef;
  border-radius: 5px;
  white-space: nowrap;
}
.status-completed {
  color: #197151;
  background: #e5f3eb;
}
.status-failed,
.status-interrupted {
  color: #ad493a;
  background: #fff0eb;
}
.status-waiting_human,
.status-waiting_external {
  color: #997520;
  background: #faf3dc;
}
.research-row-actions {
  display: flex;
  flex-direction: column;
  gap: 14px;
}
.research-new {
  padding: 9px 13px;
  background: #22775e;
  color: white;
  border-radius: 8px;
  text-decoration: none;
}
.research-row-actions button {
  border: 1px solid #dce5df;
  background: white;
  border-radius: 6px;
  padding: 5px 10px;
  color: #ad493a;
  cursor: pointer;
}
.research-row-actions a {
  display: flex;
  gap: 6px;
  align-items: center;
  color: #22775e;
  text-decoration: none;
  white-space: nowrap;
}
.research-row-actions a:hover {
  text-decoration: underline;
}
.research-error,
.research-row-error {
  color: #ad493a;
  overflow-wrap: anywhere;
}
.research-error {
  padding: 16px 28px;
}
.research-empty {
  text-align: center;
  height: 130px;
  color: #7d8c84;
}
.research-pagination {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 18px 28px;
  border-top: 1px solid #e6ece8;
}
.research-pagination div {
  display: flex;
  gap: 8px;
}
button:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
@media (max-width: 600px) {
  .research-heading,
  .research-toolbar {
    padding: 18px;
  }
  .research-dialog {
    width: calc(100vw - 16px);
  }
  .research-table-wrap {
    padding: 0 12px;
  }
}
</style>
