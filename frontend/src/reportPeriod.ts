export function latestReportWeek(): string {
  // 与后端一致按 UTC 取已结束周；当天是周六时仍退到上周，避免使用未结束数据。
  const day = new Date();
  day.setUTCDate(day.getUTCDate() - ((day.getUTCDay() + 1) % 7 || 7));
  return day.toISOString().slice(0, 10);
}

export function periodLabel(period: {
  period_kind?: string;
  period_start: string | null;
  period_end: string | null;
  collected_at?: string | null;
}): string {
  // 最近 30 天显示供应商口径与采集时间，不在浏览器端捏造固定起止日期。
  if (period.period_kind === 'last_30_days') {
    return `最近 30 天${period.collected_at ? ` · 采集于 ${new Date(period.collected_at).toLocaleString()}` : ''}`;
  }
  return `${period.period_start ?? '未知'} ～ ${period.period_end ?? '未知'}`;
}
