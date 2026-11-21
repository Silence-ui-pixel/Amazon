import assert from 'node:assert/strict';

import { createSSRApp, h } from 'vue';
import { renderToString } from '@vue/server-renderer';

import AsinBatchSelector, { nextAsinSelection } from '../src/components/AsinBatchSelector.ts';

const asins = Array.from({ length: 21 }, (_, index) => `B${String(index + 1).padStart(9, '0')}`);

assert.deepEqual(nextAsinSelection([], asins[0], true, 20), [asins[0]]);
assert.deepEqual(nextAsinSelection([asins[0]], asins[1], true, 20), [asins[0], asins[1]]);
assert.deepEqual(nextAsinSelection([asins[0], asins[1]], asins[0], false, 20), [asins[1]]);
assert.deepEqual(nextAsinSelection(asins.slice(0, 20), asins[20], true, 20), asins.slice(0, 20));

const html = await renderToString(
  createSSRApp({
    render: () =>
      h(AsinBatchSelector, {
        options: asins,
        modelValue: asins.slice(0, 20),
        max: 20,
      }),
  }),
);

assert.equal((html.match(/type="checkbox"/g) ?? []).length, 21);
assert.match(html, new RegExp(`value="${asins[0]}"[^>]*checked`));
assert.match(html, new RegExp(`value="${asins[20]}"[^>]*disabled`));
