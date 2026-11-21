import { fetchResult } from '../resultCache';
/** 图片上传与输入提交客户端；通过同源 /api 代理访问本地后端。 */
export interface ImageAsset {
  id: string;
  project_id: string;
  original_name: string;
  content_url: string;
  width: number;
  height: number;
  size_bytes: number;
}

export interface InputRun {
  id: string;
  project_id: string;
  status: 'input_received';
}

/** MIMO 真实识别结果；推断信息需要人工核对。 */
export interface UnderstandingResult {
  project_id: string;
  run_id: string;
  provider: 'mimo' | 'bailian';
  model: string;
  created_at: string;
  product: {
    product_name: string;
    product_type: string;
    features: string[];
    uncertainties: string[];
  };
  warnings: string[];
}

/** 词根候选由服务端赋予稳定 ID；文字仅作推荐，不代表搜索证据。 */
export interface RootCandidates {
  project_id: string;
  run_id: string;
  model: string;
  language: string;
  candidates: Array<{ id: string; text: string; translation: string; reason: string }>;
  warnings: string[];
  rule_version?: 'legacy-root-v1' | 'product-term-v1';
  version?: number;
  source?: 'model' | 'manual';
  raw_response?: string;
  diagnostic_id?: string;
  validation_issues?: Array<{ text: string; code: string; message: string }>;
  history?: RootCandidates[];
}
export interface RootSelection {
  candidate_ids: string[];
  confirmed_at: string;
  source: 'manual' | 'automatic';
  verification_id: string | null;
}

/** 与后端资料契约对应；已上传图片和远端地址保留各自来源。 */
export interface ProductMaterials {
  schema_version: 1;
  project_id: string;
  run_id: string;
  marketplace: string;
  asin: string | null;
  supplied_product_name: string | null;
  supplied_description: string;
  reference_price: string | null;
  reference_currency: 'USD';
  images: Array<{
    source: 'uploaded' | 'remote_url';
    verification: 'verified_at_upload' | 'unverified';
    role: 'unspecified';
    url: string;
    asset: ImageAsset | null;
  }>;
  asin_resolution: 'awaiting_provider' | 'not_requested' | 'resolved';
  asin_product: {
    asin: string;
    title: string;
    features: string[];
    description: string;
    image_url: string;
    source_url: string;
    captured_at: string;
  } | null;
  model_status: 'not_started' | 'running' | 'succeeded' | 'failed' | 'uncertain';
  warnings: string[];
}

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

/** 保留后端可读错误，避免上传失败时页面仍显示成功。 */
export async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const headers = new Headers(init?.headers);
  const deviceId = resolveBrowserDeviceId();
  if (deviceId && url.startsWith('/api/')) {
    headers.set('X-Keyword-Browser-Device', deviceId);
  }
  const response = await fetchResult(url, { ...init, headers });
  const body: unknown = await response.json().catch(() => null);
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}），请查看服务日志或重试`;
    if (body && typeof body === 'object' && 'error' in body) {
      const error = body.error;
      if (error && typeof error === 'object' && 'message' in error) {
        message = String(error.message);
      }
    }
    throw new ApiError(message, response.status);
  }
  if (!body) throw new Error('后端未返回有效数据，请检查服务');
  return body as T;
}

/** 一次页面会话内复用项目、图片和提交键，失败重试不会重复上传成功的文件。 */
export class ImageInputClient {
  private projects = new Map<string, string>();
  private assets = new Map<string, Map<File, ImageAsset>>();
  private uploadKeys = new WeakMap<File, string>();
  private runKeys = new Map<string, string>();

  /** 读取已保存任务的资料；单独调用使查询失败不掩盖保存成功。 */
  getMaterials(run: InputRun): Promise<ProductMaterials> {
    return request<ProductMaterials>(
      `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-materials`,
    );
  }

  /** 显式触发计费调用；成功后由后端缓存，同任务重复点击复用结果。 */
  identify(run: InputRun): Promise<UnderstandingResult> {
    return request<UnderstandingResult>(
      `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-understanding`,
      { method: 'POST' },
    );
  }

  generateRoots(run: InputRun): Promise<RootCandidates> {
    return request<RootCandidates>(
      `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/product-term-candidates`,
      { method: 'POST' },
    );
  }

  /** 恢复页面只读取缓存；404 表示尚未生成，绝不隐式触发模型。 */
  private async readOptional<T>(run: InputRun, resource: string): Promise<T | null> {
    try {
      return await request<T>(
        `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/${resource}`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  }

  getUnderstanding(run: InputRun): Promise<UnderstandingResult | null> {
    return this.readOptional(run, 'product-understanding');
  }

  getRoots(run: InputRun): Promise<RootCandidates | null> {
    return this.readOptional(run, 'root-candidates');
  }

  /** 初次无确认记录是正常状态；其他读取错误必须提示，不能伪装成未选择。 */
  async getRootSelection(run: InputRun): Promise<RootSelection | null> {
    try {
      return await request<RootSelection>(
        `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/root-selection`,
      );
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  }

  /** 仅提交当前候选 ID，不允许客户端替换模型文字或伪造候选。 */
  confirmRoots(run: InputRun, candidateIds: string[]): Promise<RootSelection> {
    return request<RootSelection>(
      `/api/v1/projects/${run.project_id}/pipeline-runs/${run.id}/root-selection`,
      {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ candidate_ids: candidateIds }),
      },
    );
  }

  async submit(
    marketplace: string,
    files: File[],
    asin: string,
    description: string,
    onStatus: (file: File, status: string, asset?: ImageAsset) => void,
    referencePrice?: string,
  ): Promise<InputRun> {
    let projectId = this.projects.get(marketplace);
    if (!projectId) {
      const project = await request<{ id: string }>('/api/v1/projects', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: '商品关键词研究', marketplace }),
      });
      projectId = project.id;
      this.projects.set(marketplace, projectId);
    }
    const cache = this.assets.get(projectId) ?? new Map<File, ImageAsset>();
    this.assets.set(projectId, cache);
    const imageIds: string[] = [];
    for (const file of files) {
      let asset = cache.get(file);
      if (!asset) {
        onStatus(file, '上传中…');
        const key = this.uploadKeys.get(file) ?? crypto.randomUUID();
        this.uploadKeys.set(file, key);
        const data = new FormData();
        data.append('file', file);
        try {
          // FormData 的 boundary 由浏览器设置，不能手动指定 Content-Type。
          asset = await request<ImageAsset>(`/api/v1/projects/${projectId}/images`, {
            method: 'POST',
            headers: { 'Idempotency-Key': key },
            body: data,
          });
        } catch (error) {
          onStatus(file, '上传失败，可重试');
          throw error;
        }
        cache.set(file, asset);
      }
      imageIds.push(asset.id);
      onStatus(file, '已上传', asset);
    }
    const payload = {
      marketplace,
      ...(asin ? { asin } : {}),
      image_ids: imageIds,
      description,
      ...(referencePrice ? { reference_price: referencePrice, reference_currency: 'USD' } : {}),
    };
    const fingerprint = JSON.stringify({ projectId, ...payload });
    const key = this.runKeys.get(fingerprint) ?? crypto.randomUUID();
    this.runKeys.set(fingerprint, key);
    return request<InputRun>(`/api/v1/projects/${projectId}/pipeline-runs`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
      body: JSON.stringify(payload),
    });
  }
}
import { resolveBrowserDeviceId } from '../browserDevice';

