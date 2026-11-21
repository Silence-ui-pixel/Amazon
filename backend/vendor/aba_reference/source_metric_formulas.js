'use strict';

function abaMetricFormulas(row) {
  return {
    purchaseRatio: `IFERROR(N${row}/M${row},"")`,
    conversionSum3: `IF(COUNT(N${row},R${row},V${row})<3,"",SUM(N${row},R${row},V${row}))`,
    purchaseLabel: `IF(C${row}="","待补数据",IF(C${row}>60%,"购买型","非购买型"))`,
    monopolyLabel: `IF(N${row}>25%,"垄断词",IF(D${row}="","待补数据",IF(D${row}>35%,"垄断词","非垄断词")))`,
  };
}

function sellerMinerMetricFormulas(row) {
  return {
    purchaseRatio: `IFERROR(AE${row}/AD${row},"")`,
    conversionSum3: `IF(COUNT(AE${row},AH${row},AK${row})<3,"",SUM(AE${row},AH${row},AK${row}))`,
    purchaseLabel: `IF(C${row}="","待补数据",IF(C${row}>60%,"购买型","非购买型"))`,
    monopolyLabel: `IF(AE${row}>25%,"垄断词",IF(D${row}="","待补数据",IF(D${row}>35%,"垄断词","非垄断词")))`,
  };
}

function sellerReverseMetricLabels(top3ConversionShare) {
  if (top3ConversionShare == null || top3ConversionShare === '') {
    return { purchaseLabel: '待补数据', monopolyLabel: '待补数据' };
  }
  const value = Number(top3ConversionShare);
  return {
    purchaseLabel: '待补数据',
    monopolyLabel:
      Number.isFinite(value) && value > 0.35 ? '垄断词' : '待补第一名转化份额',
  };
}

module.exports = {
  abaMetricFormulas,
  sellerMinerMetricFormulas,
  sellerReverseMetricLabels,
};
