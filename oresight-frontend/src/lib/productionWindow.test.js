// Run with:  node --test src/lib/productionWindow.test.js   (Node's built-in runner; no extra dependency)
// Lives under src/ so Playwright (testDir ./tests, live-API smoke tests) never picks it up.
import test from 'node:test';
import assert from 'node:assert/strict';
import { averageVariancePct, noDataMessage, summarizeOutputVariance, PRODUCTION_WINDOW_DAYS } from './productionWindow.js';

test('empty list never produces NaN: shows a dash and the no-data sentence', () => {
  const s = summarizeOutputVariance([]);
  assert.equal(s.value, '—');
  assert.equal(s.foot, `No data in the last ${PRODUCTION_WINDOW_DAYS} days`);
  assert.equal(s.hasData, false);
  assert.ok(!/NaN/.test(`${s.value}${s.foot}`));
});

test('null / undefined input is treated as empty', () => {
  assert.equal(averageVariancePct(null), null);
  assert.equal(averageVariancePct(undefined), null);
  assert.equal(summarizeOutputVariance(undefined).hasData, false);
});

test('rows without a usable variance_pct do not poison the average', () => {
  assert.equal(averageVariancePct([{ variance_pct: null }, { variance_pct: undefined }, { variance_pct: NaN }]), null);
  assert.equal(averageVariancePct([{ variance_pct: -4 }, { variance_pct: null }, { variance_pct: -6 }]), -5);
});

test('signed formatting: positive gets +, negative keeps its minus', () => {
  assert.equal(summarizeOutputVariance([{ variance_pct: 2 }, { variance_pct: 3 }]).value, '+2.5%');
  assert.equal(summarizeOutputVariance([{ variance_pct: -12.34 }]).value, '-12.3%');
  assert.equal(summarizeOutputVariance([{ variance_pct: 0 }]).value, '0.0%');
});

test('the footer names the window the mean is actually taken over (not "7-day")', () => {
  assert.equal(summarizeOutputVariance([{ variance_pct: -4 }]).foot, `${PRODUCTION_WINDOW_DAYS}-day average`);
  assert.equal(summarizeOutputVariance([{ variance_pct: -4 }], 14).foot, '14-day average');
});

test('custom window length flows into the message', () => {
  assert.equal(noDataMessage(14), 'No data in the last 14 days');
  assert.equal(summarizeOutputVariance([], 14).foot, 'No data in the last 14 days');
});
