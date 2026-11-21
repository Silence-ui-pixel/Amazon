const assert = require('node:assert/strict');
const {
  chromium,
} = require('C:/Users/Administrator/.cache/codex-runtimes/codex-primary-runtime/dependencies/node/node_modules/playwright');

const project = '11111111-1111-4111-8111-111111111111';
const run = {
  id: '22222222-2222-4222-8222-222222222222',
  project_id: project,
  status: 'input_received',
};
const image = {
  id: '33333333-3333-4333-8333-333333333333',
  project_id: project,
  original_name: 'product-photo.jpg',
  content_url: '/offline-product-photo.jpg',
  width: 800,
  height: 800,
  size_bytes: 1024,
};

(async () => {
  const browser = await chromium.launch({ headless: true });
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
    await page.route('**/api/v1/**', async (route) => {
      const url = new URL(route.request().url()).pathname;
      let data;
      if (url.endsWith('/projects')) data = { id: project };
      else if (url.endsWith('/images')) data = image;
      else if (url.endsWith('/pipeline-runs')) data = run;
      else if (url.endsWith('/product-materials')) {
        data = {
          marketplace: 'US',
          asin: null,
          reference_price: '20.00',
          reference_currency: 'USD',
          images: [{ source: 'uploaded', asset: image, url: image.content_url }],
          warnings: [],
          asin_resolution: 'not_requested',
          model_status: 'succeeded',
        };
      } else if (url.endsWith('/product-understanding')) {
        data = {
          project_id: project,
          run_id: run.id,
          provider: 'bailian',
          model: 'qwen3.8-max',
          product: {
            product_name: '示例商品',
            product_type: '测试商品',
            features: [],
            uncertainties: [],
          },
          warnings: [],
        };
      } else {
        await route.fulfill({ status: 404, json: { error: { message: 'offline fixture' } } });
        return;
      }
      await route.fulfill({ status: 200, json: data });
    });

    await page.goto('http://127.0.0.1:5173/?new=1');
    await page.getByLabel('选择 Amazon 站点').selectOption('US');
    await page.locator('input[type=file]').setInputFiles(process.argv[2]);
    await page.getByLabel(/本品参考售价/).fill('20');
    await page.getByRole('button', { name: '保存商品', exact: true }).click();
    await page.getByRole('button', { name: '识别商品 · Qwen 3.8 Max', exact: true }).click();
    await page.getByLabel('搜索核验主图').selectOption(image.id);

    const preview = page.getByRole('img', {
      name: `当前搜索核验主图：${image.original_name}`,
    });
    await preview.waitFor({ timeout: 3000 });
    assert.equal(await preview.getAttribute('src'), image.content_url);
    assert.equal(await page.locator('.reference-image-preview figcaption').textContent(), image.original_name);
    console.log(JSON.stringify({ selectedReferenceImageVisible: true }));
  } finally {
    await browser.close();
  }
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
