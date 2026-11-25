import { describe, expect, it } from 'vitest';
import { canonicalSummary, coverageGuidance, coverageSummary, isPhraseGraph } from './coverage';
import { fullCoverage, health, makeFullResult, makeResult } from './test/fixtures';
import { colorType } from './Graph';

describe('coverage and graph interpretation', () => {
  it('assumes no source count before coverage is reported', () => {
    expect(coverageSummary(null)).toBe('Collection coverage not yet confirmed');
    expect(coverageGuidance(undefined)).toContain('No source count is assumed');
    expect(canonicalSummary(null)).toBeNull();
  });

  it('distinguishes indexed coverage, canonical records and per-answer retrieval', () => {
    expect(coverageSummary(fullCoverage)).toBe('All 1,060 sources indexed');
    expect(canonicalSummary(fullCoverage)).toBe('1,059 canonical speeches');
    expect(coverageGuidance(fullCoverage)).toContain('not every indexed source');
    expect(canonicalSummary({ ...fullCoverage, canonical_count: undefined })).toBeNull();
  });

  it('keeps legacy sample coverage explicitly limited and count-driven', () => {
    expect(coverageSummary(health.coverage)).toBe('10 sources · limited sample');
    expect(coverageGuidance(health.coverage)).toContain('full corpus is unavailable');
    expect(coverageSummary({ ...health.coverage, source_count: 7 })).toBe('7 sources · limited sample');
  });

  it('uses the exact-phrase interpretation for full coverage even without basis', () => {
    expect(isPhraseGraph(makeResult().graph, fullCoverage)).toBe(true);
    expect(isPhraseGraph(makeFullResult().graph, health.coverage)).toBe(true);
    expect(isPhraseGraph(makeResult().graph, health.coverage)).toBe(false);
    expect(isPhraseGraph(null, null)).toBe(false);
    expect(colorType('Passage')).toBe('passage');
  });
});
