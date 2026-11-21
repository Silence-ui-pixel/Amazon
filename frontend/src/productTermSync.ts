import type { InputRun } from './api/images';
import type { ProductTermAnalysis } from './api/productTermAnalysis';
import { usePanelSync } from './panelSync';

type ProductTermSyncOptions = {
  revision: () => unknown;
  blocked: () => boolean;
  run: () => InputRun | null;
  read: (run: InputRun) => Promise<ProductTermAnalysis | null>;
  accept: (analysis: ProductTermAnalysis | null) => void;
};

/** 自动编排状态变化后补读商品词分析，避免必须刷新页面才能看到相关度。 */
export function useProductTermAnalysisSync(options: ProductTermSyncOptions) {
  return usePanelSync(options.revision, options.blocked, async () => {
    const run = options.run();
    if (!run) return;
    const analysis = await options.read(run);
    // 切换研究记录后，旧请求不得覆盖新记录的页面状态。
    if (options.run()?.id !== run.id) return;
    options.accept(analysis);
  });
}
