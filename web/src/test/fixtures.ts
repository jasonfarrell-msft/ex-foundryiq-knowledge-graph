import type { AskResult, Catalog, Health } from '../types';

// Synthetic test-only assertions. Never imported by production components.
export const health: Health = {
  status: 'ok',
  configured: true,
  azure_connectivity: 'not_probed',
  coverage: { label: 'Ten-speech sample', source_count: 10, full_corpus_available: false },
  limits: { question_characters: 2000, graph_edges: 60 },
  warnings: [],
};

export const catalog: Catalog = {
  speeches: [{ id: 'test-source', title: 'Test inaugural', speaker: 'Test speaker', date: '1933-03-04' }],
  coverage: health.coverage,
  warnings: [],
};

export const fullCoverage = {
  label: 'Full presidential speech corpus',
  source_count: 1060,
  canonical_count: 1059,
  full_corpus_available: true,
};

export const fullHealth: Health = { ...health, coverage: fullCoverage };

export function makeFullCatalog(): Catalog {
  return {
    coverage: fullCoverage,
    warnings: [],
    speeches: Array.from({ length: 1060 }, (_, index) => ({
      id: `test-full-source-${index}`,
      title: index === 1059 ? 'Gettysburg Address test record' : `Synthetic speech ${String(index + 1).padStart(4, '0')}`,
      speaker: index === 1059 ? 'Abraham Lincoln' : `Test speaker ${index % 10}`,
      date: index === 1059 ? '1863-11-19' : '1900-01-01',
    })),
  };
}

export function makeFullResult(): AskResult {
  return makeResult({
    coverage: fullCoverage,
    citations: makeResult().citations.map((citation, index) => index === 0
      ? { ...citation, canonical_id: 'canonical/speech:1' }
      : citation),
    graph: {
      scope: 'cited_passages',
      basis: 'source_and_exact_topic_phrase_matches',
      truncated: false,
      nodes: [
        { id: 'canonical/speech:1', type: 'Speech', label: 'First test speech' },
        { id: 'opaque/passage:1', type: 'Passage', label: 'Cited passage' },
        { id: 'curated/topic:work', type: 'Topic', label: 'Work' },
      ],
      edges: [
        { id: 'opaque/edge:source', source: 'canonical/speech:1', target: 'opaque/passage:1', label: 'containsPassage', evidence_ids: ['0'] },
        { id: 'opaque/edge:phrase', source: 'opaque/passage:1', target: 'curated/topic:work', label: 'contains phrase "work"', evidence_ids: ['0'] },
      ],
    },
  });
}

export function makeResult(overrides: Partial<AskResult> = {}): AskResult {
  return {
    request_id: 'test-request-1',
    question: 'How are the test topics connected?',
    answer: 'The test passage connects work and reform [ref_id:0].\n\nRead this unknown reference [ref_id:unknown].',
    citations: [
      { id: '0', title: 'First test speech', speaker: 'Test speaker', date: '1933-03-04', chunk_id: 'opaque/chunk:1', source_id: 'test-source-1', text: 'Work and reform appear together in this synthetic test passage.' },
      { id: 'evidence:b', title: 'Second test speech', speaker: null, date: null, chunk_id: 'opaque/chunk:2', source_id: null, text: '<script>not executable</script> A second synthetic test quotation.' },
    ],
    graph: {
      scope: 'cited_passages',
      truncated: false,
      nodes: [
        { id: 'node:opaque:1', label: 'Reform', type: 'Topic' },
        { id: 'node:opaque:2', label: 'Employment', type: 'Topic' },
        { id: 'node:opaque:3', label: 'Reform', type: 'Speech' },
      ],
      edges: [
        { id: 'edge:opaque:1', source: 'node:opaque:1', target: 'node:opaque:2', label: 'reported alongside', evidence_ids: ['0'], confidence: 0 },
        { id: 'edge:opaque:2', source: 'node:opaque:1', target: 'node:opaque:2', label: 'reported alongside', evidence_ids: ['evidence:b'], confidence: null },
      ],
    },
    warnings: [],
    coverage: health.coverage,
    timing_ms: 17_100,
    partial: false,
    ...overrides,
  };
}

export function json(data: unknown, status = 200, headers: Record<string, string> = {}) {
  return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}
