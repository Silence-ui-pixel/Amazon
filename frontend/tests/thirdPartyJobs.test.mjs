import assert from 'node:assert/strict';

import { normalizeBrowserJobs } from '../src/api/thirdPartyJobs.ts';

const jobs = normalizeBrowserJobs([
  {
    id: 'legacy',
    request: { direction: 'reverse', query: 'B012345678', month: '2026-09' },
    status: 'succeeded',
    stage: '已完成',
    error: '',
    report_id: null,
  },
  {
    id: 'batch',
    request: {
      direction: 'reverse',
      query: 'B012345678,B087654321',
      method: 'traffic_extension',
      queries: ['B012345678', 'B087654321'],
      variant_scope: 'all',
      period: 'last_30_days',
    },
    status: 'running',
    stage: '查询中',
    error: '',
    report_id: null,
  },
]);

assert.equal(jobs.length, 2);
assert.deepEqual(jobs[0].request, {
  direction: 'reverse',
  query: 'B012345678',
  method: 'keyword_reverse',
  queries: ['B012345678'],
  variant_scope: null,
  month: '2026-09',
});
assert.equal(jobs[1].request.method, 'traffic_extension');
assert.throws(() => normalizeBrowserJobs([{ id: 'broken' }]));
