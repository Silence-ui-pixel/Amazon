import { ref, watch } from 'vue';

/** Queue one read after background changes; never replace a draft or interrupt a save. */
export function usePanelSync(
  revision: () => unknown,
  blocked: () => boolean,
  refresh: () => Promise<unknown>,
) {
  // revision 只是刷新通知；blocked 由面板的未保存草稿/保存中状态决定。
  // 多次通知合并为一次读取，避免后台进度更新覆盖用户正在编辑的内容。
  const pending = ref(false);
  const reading = ref(false);
  watch(revision, () => {
    pending.value = true;
  });
  watch([pending, blocked, reading], async () => {
    if (!pending.value || blocked() || reading.value) return;
    // 读取前消费本次通知；读取期间的新通知会再次置位，结束后仍能补读。
    pending.value = false;
    reading.value = true;
    try {
      await refresh();
    } finally {
      reading.value = false;
    }
  });
  return pending;
}
