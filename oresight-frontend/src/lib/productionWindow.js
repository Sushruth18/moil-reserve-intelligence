// Production-window helpers shared by the dashboard and site pages.
//
// GET /production?days=N returns only rows from the last N days. A demo database whose data ends
// months ago (or a brand-new site) legitimately returns [] -- the views must say so instead of
// dividing by zero ("NaN%") or drawing an empty grey chart box.

/** Days of production history the dashboard / site workspace request. Single source for client.js and the UI copy. */
export const PRODUCTION_WINDOW_DAYS = 30;

/** "No data in the last 30 days" -- the one empty-state sentence used everywhere a production window is empty. */
export const noDataMessage = (days = PRODUCTION_WINDOW_DAYS) => `No data in the last ${days} days`;

/** Mean of the finite variance_pct values, or null when there are none (never NaN). */
export function averageVariancePct(records) {
  const values = (records || []).map((r) => r?.variance_pct).filter((v) => typeof v === 'number' && Number.isFinite(v));
  return values.length ? values.reduce((a, b) => a + b, 0) / values.length : null;
}

/** Value/foot for the "Output variance" stat card. `value` is a display string; never "NaN%". */
export function summarizeOutputVariance(records, days = PRODUCTION_WINDOW_DAYS) {
  const avg = averageVariancePct(records);
  if (avg === null) return { value: '—', foot: noDataMessage(days), hasData: false };
  // The mean is taken over every row in the requested window (`days`), so the label says that, not "7-day".
  return { value: `${avg > 0 ? '+' : ''}${avg.toFixed(1)}%`, foot: `${days}-day average`, hasData: true };
}
