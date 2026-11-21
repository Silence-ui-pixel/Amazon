const test = require('node:test');
const assert = require('node:assert/strict');

const {
  abaMetricFormulas,
  sellerMinerMetricFormulas,
  sellerReverseMetricLabels,
} = require('../vendor/aba_reference/source_metric_formulas');

test('ABA 来源行在原表计算购买性和垄断性判断', () => {
  assert.deepEqual(abaMetricFormulas(3), {
    purchaseRatio: 'IFERROR(N3/M3,"")',
    conversionSum3: 'IF(COUNT(N3,R3,V3)<3,"",SUM(N3,R3,V3))',
    purchaseLabel: 'IF(C3="","待补数据",IF(C3>60%,"购买型","非购买型"))',
    monopolyLabel:
      'IF(N3>25%,"垄断词",IF(D3="","待补数据",IF(D3>35%,"垄断词","非垄断词")))',
  });
});

test('卖家精灵正向来源行沿用原始列计算判断', () => {
  assert.deepEqual(sellerMinerMetricFormulas(2), {
    purchaseRatio: 'IFERROR(AE2/AD2,"")',
    conversionSum3: 'IF(COUNT(AE2,AH2,AK2)<3,"",SUM(AE2,AH2,AK2))',
    purchaseLabel: 'IF(C2="","待补数据",IF(C2>60%,"购买型","非购买型"))',
    monopolyLabel:
      'IF(AE2>25%,"垄断词",IF(D2="","待补数据",IF(D2>35%,"垄断词","非垄断词")))',
  });
});

test('卖家精灵反向缺少首位份额时不伪造购买性或非垄断结论', () => {
  assert.deepEqual(sellerReverseMetricLabels(null), {
    purchaseLabel: '待补数据',
    monopolyLabel: '待补数据',
  });
  assert.deepEqual(sellerReverseMetricLabels(0.36), {
    purchaseLabel: '待补数据',
    monopolyLabel: '垄断词',
  });
  assert.deepEqual(sellerReverseMetricLabels(0.35), {
    purchaseLabel: '待补数据',
    monopolyLabel: '待补第一名转化份额',
  });
});
