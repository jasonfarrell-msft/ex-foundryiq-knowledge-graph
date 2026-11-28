import type { AskResult, Coverage } from './types';

const number = new Intl.NumberFormat('en-US');
export const formatCount = (value: number) => number.format(value);

export function coverageSummary(coverage: Coverage | null | undefined): string {
  if (!coverage) return 'Collection coverage not yet confirmed';
  return coverage.full_corpus_available
    ? `All ${formatCount(coverage.source_count)} sources indexed`
    : `${formatCount(coverage.source_count)} sources · limited sample`;
}

export function canonicalSummary(coverage: Coverage | null | undefined): string | null {
  return coverage?.canonical_count !== undefined
    ? `${formatCount(coverage.canonical_count)} canonical speeches`
    : null;
}

export function coverageGuidance(coverage: Coverage | null | undefined): string {
  if (!coverage) return 'Coverage will be reported by the service. No source count is assumed.';
  return coverage.full_corpus_available
    ? 'Full corpus available for retrieval. Each answer uses a retrieved subset, not every indexed source.'
    : 'Limited sample only; the full corpus is unavailable in this collection. Missing evidence is not evidence of absence.';
}

export function isPhraseGraph(graph: AskResult['graph'] | null, coverage: Coverage | null | undefined): boolean {
  if (graph?.basis === 'foundry_graph_index') return false;
  // The basis is explicit when supplied. Full-corpus responses use the
  // conservative source / passage / case-insensitive literal phrase interpretation even if omitted.
  return graph?.basis === 'source_and_exact_topic_phrase_matches' || coverage?.full_corpus_available === true;
}
