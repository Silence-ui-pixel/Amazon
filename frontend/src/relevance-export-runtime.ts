/** Compatibility entry for deployments whose existing bundle differs from saved source.
 * Mount only at the old Excel link. A full build renders KeywordLibraryResults directly
 * and does not load this entry. Existing views, Vue state and other bundles are retained.
 */
import type { LibraryRelevanceJob } from './api/library';
import type { InputRun } from './api/images';

// Read the existing page's current persisted device selection on every request.
// This entry must not create a second copy of the page's Vue/device state.
async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  try {
    const device =
      localStorage.getItem('keyword.agent.selectedDevice') ||
      localStorage.getItem('keyword.agent.deviceId');
    if (device) headers.set('X-Keyword-Browser-Device', device);
  } catch {
    /* Server fallback still applies when browser storage is unavailable. */
  }
  const response = await fetch(url, { ...init, headers });
  const body = await response.json().catch(() => null);
  if (!response.ok) throw new Error(body?.error?.message || `请求失败（HTTP ${response.status}）`);
  if (!body) throw new Error('后端未返回有效数据，请检查服务');
  return body as T;
}
const base = (run: InputRun, id: string) =>
  `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/library/${encodeURIComponent(id)}/relevance-exports`;
const libraryApi = {
  relevanceLatest: (run: InputRun, id: string, ranked: boolean) =>
    request<{ job: LibraryRelevanceJob | null }>(
      `${base(run, id)}/latest?ranked_only=${ranked}`,
    ).then((v) => v.job),
  relevanceGet: (run: InputRun, id: string, job: string) =>
    request<LibraryRelevanceJob>(`${base(run, id)}/${encodeURIComponent(job)}`),
  relevanceStart: (run: InputRun, id: string, ranked: boolean) =>
    request<LibraryRelevanceJob>(base(run, id), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ranked_only: ranked }),
    }),
  relevanceRetry: (run: InputRun, id: string, job: string) =>
    request<LibraryRelevanceJob>(`${base(run, id)}/${encodeURIComponent(job)}/retry`, {
      method: 'POST',
    }),
  relevanceFile: (run: InputRun, id: string, job: string) =>
    `${base(run, id)}/${encodeURIComponent(job)}/file`,
};

interface Scope {
  run: InputRun;
  library: string;
  ranked: boolean;
  version: number;
}
interface Controller {
  link: HTMLAnchorElement;
  panel: HTMLElement;
  update: () => void;
  dispose: () => void;
}
const controllers = new Set<Controller>();
const bound = new WeakSet<HTMLAnchorElement>();

function mount(link: HTMLAnchorElement): Controller | null {
  const host = link.closest('#library');
  if (!host) return null;
  const panel = document.createElement('section');
  panel.setAttribute('aria-label', 'Excel相关性核验');
  const generate = document.createElement('button');
  generate.type = 'button';
  generate.textContent = '计算相关性并生成 Excel';
  const status = document.createElement('p');
  status.setAttribute('role', 'status');
  const error = document.createElement('p');
  error.setAttribute('role', 'alert');
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.textContent = '重试未完成核验';
  const download = document.createElement('a');
  download.className = 'template-download';
  download.textContent = '下载关键词库及来源附表 Excel（相关性百分比）';
  const note = document.createElement('p');
  note.className = 'muted';
  note.textContent =
    '相关性 = 高相关商品数 ÷ 有效核验商品数；每词目标 30 个自然商品。首次核验需要采集设备在线，完成后可重复下载。';
  panel.append(generate, status, error, retry, download, note);
  for (const attribute of link.attributes) {
    if (!attribute.name.startsWith('data-v-')) continue;
    for (const element of [panel, ...panel.querySelectorAll('*')]) {
      element.setAttribute(attribute.name, attribute.value);
    }
  }
  link.after(panel);
  link.hidden = true;
  link.style.display = 'none';
  let scope: Scope | null = null;
  let signature = '';
  let ticket = 0;
  let disposed = false;
  let submitting = false;
  let job: LibraryRelevanceJob | null = null;
  let message = '';
  let timer: ReturnType<typeof setTimeout> | undefined;

  function active() {
    return job?.status === 'queued' || job?.status === 'running';
  }
  function render() {
    const locked =
      host!.querySelector<HTMLSelectElement>('[aria-label="入库版本"]')?.disabled ?? false;
    const disabled = locked || submitting || active() || !scope;
    if (generate.disabled !== disabled) generate.disabled = disabled;
    if (retry.disabled !== disabled) retry.disabled = disabled;
    const label = submitting ? '正在启动…' : active() ? '正在核验商品…' : '计算相关性并生成 Excel';
    if (generate.textContent !== label) generate.textContent = label;
    const progress = job
      ? `已完成 ${job.completed} / ${job.total} 个关键词；失败 ${job.failed} 个。已处理词的有效样本缺口 ${job.missing_samples} 个（每词目标 30 个）。${job.current_keyword ? `当前${job.phase === 'collecting' ? '采集' : '核验'}：${job.current_keyword}` : ''}${job.status === 'completed' ? '相关性核验完成，可以下载。' : ''}`
      : '';
    if (status.textContent !== progress) status.textContent = progress;
    const text = message || job?.error || '';
    if (error.textContent !== text) error.textContent = text;
    retry.hidden = job?.status !== 'failed';
    download.hidden = job?.status !== 'completed';
    retry.style.display = job?.status === 'failed' ? '' : 'none';
    download.style.display = job?.status === 'completed' ? 'inline-block' : 'none';
    if (job?.status === 'completed' && scope) {
      const href = libraryApi.relevanceFile(scope.run, scope.library, job.id);
      if (download.getAttribute('href') !== href) download.setAttribute('href', href);
    }
  }
  function poll(epoch: number) {
    if (disposed || epoch !== ticket || !active()) return;
    timer = setTimeout(async () => {
      const current = scope;
      if (!current || !job || disposed || epoch !== ticket) return;
      try {
        const result = await libraryApi.relevanceGet(current.run, current.library, job.id);
        if (disposed || epoch !== ticket) return;
        job = result;
        message = '';
      } catch (e) {
        if (disposed || epoch !== ticket) return;
        message = e instanceof Error ? e.message : '核验进度读取失败';
      }
      render();
      poll(epoch);
    }, 3000);
  }
  async function start(isRetry: boolean) {
    const current = scope;
    if (!current || generate.disabled || disposed) return;
    const epoch = ++ticket;
    clearTimeout(timer);
    submitting = true;
    message = '';
    render();
    try {
      const result =
        isRetry && job
          ? await libraryApi.relevanceRetry(current.run, current.library, job.id)
          : await libraryApi.relevanceStart(current.run, current.library, current.ranked);
      if (disposed || epoch !== ticket) return;
      job = result;
      poll(epoch);
    } catch (e) {
      if (disposed || epoch !== ticket) return;
      message = e instanceof Error ? e.message : '相关性核验启动失败';
    } finally {
      if (!disposed && epoch === ticket) {
        submitting = false;
        render();
      }
    }
  }
  generate.addEventListener('click', () => {
    void start(false);
  });
  retry.addEventListener('click', () => {
    void start(true);
  });

  function update() {
    const url = new URL(link.href, location.href);
    const parts = url.pathname.match(
      /^\/api\/v1\/projects\/([^/]+)\/pipeline-runs\/([^/]+)\/library\/([^/]+)\/template\.xlsx$/,
    );
    const versionText =
      [...host!.querySelectorAll('p')].find((p) => /草稿修订\s+\d+/.test(p.textContent ?? ''))
        ?.textContent ?? '';
    const version = Number(versionText.match(/草稿修订\s+(\d+)/)?.[1] ?? 0);
    const next = parts ? JSON.stringify([url.pathname, url.search, version]) : '';
    if (next === signature) {
      render();
      return;
    }
    signature = next;
    clearTimeout(timer);
    const epoch = ++ticket;
    job = null;
    message = '';
    submitting = false;
    scope = parts
      ? {
          run: { project_id: parts[1]!, id: parts[2]!, status: 'input_received' },
          library: parts[3]!,
          ranked: url.searchParams.get('ranked_only') !== 'false',
          version,
        }
      : null;
    render();
    const current = scope;
    if (!current) return;
    void libraryApi
      .relevanceLatest(current.run, current.library, current.ranked)
      .then((result) => {
        if (disposed || epoch !== ticket) return;
        job =
          result && (!current.version || result.library_version === current.version)
            ? result
            : null;
        render();
        poll(epoch);
      })
      .catch((e: unknown) => {
        if (disposed || epoch !== ticket) return;
        message = e instanceof Error ? e.message : '核验任务读取失败';
        render();
      });
  }
  update();
  return {
    link,
    panel,
    update,
    dispose: () => {
      disposed = true;
      ticket++;
      clearTimeout(timer);
      panel.remove();
    },
  };
}
function synchronize() {
  for (const controller of controllers) {
    if (!controller.link.isConnected) {
      controller.dispose();
      controllers.delete(controller);
    } else controller.update();
  }
  for (const link of document.querySelectorAll<HTMLAnchorElement>(
    '#library a.template-download[href*="/template.xlsx"]',
  )) {
    if (bound.has(link)) continue;
    bound.add(link);
    const controller = mount(link);
    if (controller) controllers.add(controller);
  }
}
const observer = new MutationObserver(synchronize);
observer.observe(document.body, {
  childList: true,
  subtree: true,
  attributes: true,
  attributeFilter: ['href', 'disabled'],
});
synchronize();
