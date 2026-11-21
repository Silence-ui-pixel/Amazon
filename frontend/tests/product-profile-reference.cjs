const assert = require('node:assert/strict');
const fs = require('node:fs');
const ts = require('typescript');

function loadApi(request) {
  const source = fs.readFileSync('src/api/productProfiles.ts', 'utf8');
  const code = ts.transpileModule(source, {
    compilerOptions: { module: ts.ModuleKind.CommonJS },
  }).outputText;
  const mod = { exports: {} };
  new Function('require', 'module', 'exports', code)(
    () => ({ ApiError: class ApiError extends Error {}, request }),
    mod,
    mod.exports,
  );
  return mod.exports;
}

(async () => {
  const calls = [];
  const api = loadApi(async (url, options) => {
    calls.push({ url, options });
    return {};
  });
  const run = { id: 'run-1', project_id: 'project-1' };

  await api.generateProductProfile(run, '30.50');
  assert.equal(calls[0].url, '/api/v1/projects/project-1/pipeline-runs/run-1/product-profile');
  assert.equal(calls[0].options.method, 'POST');
  assert.equal(calls[0].options.headers['Content-Type'], 'application/json');
  assert.deepEqual(JSON.parse(calls[0].options.body), {
    reference_price: '30.50',
    currency: 'USD',
  });

  await api.generateProductProfile(run);
  assert.deepEqual(calls[1].options, { method: 'POST' });
  console.log('Product profile reference price API passed');
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
