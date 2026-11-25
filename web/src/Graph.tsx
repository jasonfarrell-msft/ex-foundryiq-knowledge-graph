import { lazy, Suspense } from 'react';
import { ErrorBoundary } from 'react-error-boundary';
import type { AskResult, Coverage, Selection } from './types';
import { isPhraseGraph } from './coverage';

const GraphCanvas = lazy(() => import('./GraphCanvas'));

interface GraphProps {
  graph: AskResult['graph'] | null;
  coverage?: Coverage | null;
  selection: Selection;
  onSelect: (selection: Selection) => void;
  loading: boolean;
  edgeLimit: number;
  selectedEvidenceCount: number;
  onViewEvidence: () => void;
}

export function Graph({ graph, coverage, selection, onSelect, loading, edgeLimit, selectedEvidenceCount, onViewEvidence }: GraphProps) {
  const hasNodes = !!graph?.nodes.length;
  const phraseGraph = isPhraseGraph(graph, coverage);
  const linkName = phraseGraph ? 'links' : 'relationships';
  const nodesById = new Map(graph?.nodes.map(node => [node.id, node]) ?? []);
  const types = new Map<string, number>();
  for (const node of graph?.nodes ?? []) types.set(node.type, (types.get(node.type) ?? 0) + 1);
  return (
    <section className="graph-panel" aria-labelledby="graph-heading">
      <div className="panel-heading">
        <div><span className="eyebrow">02 / Explore the cited passages</span><h2 id="graph-heading">{phraseGraph ? 'Sources & phrase matches' : 'Passage graph'}</h2></div>
        <span className="subtle-pill">{graph?.nodes.length ?? 0} nodes · {graph?.edges.length ?? 0} {linkName}</span>
      </div>
      <p className="graph-disclaimer">{phraseGraph ? 'Cited sources, passages, and case-insensitive exact phrase matches only—not a whole-corpus or validated semantic graph.' : 'Graph relationships in cited passages; not proof of every answer claim.'}</p>
      {phraseGraph && <details className="graph-method"><summary>What these links mean</summary><p>Canonical speech → Passage means source containment; duplicate source records share the canonical Speech node while citations retain source identity.</p><p>Passage → Topic means a case-insensitive, whole-word literal curated phrase matched in that passage. There is no stemming or fuzzy matching. Edge labels preserve the original matched case; “exact” does not mean byte-for-byte identity.</p><p>Shared topic IDs align curated aliases across sources; they do not establish author intent, semantic relatedness, or agreement. This view may be assembled separately from answer synthesis and does not prove every answer claim.</p></details>}
      <div className="graph-stage" aria-label={phraseGraph ? 'Interactive graph of cited sources, passages and case-insensitive exact phrase matches' : 'Interactive graph of reported relationships'} aria-busy={loading}>
        {hasNodes && graph ? (
          <ErrorBoundary fallback={<div className="graph-empty" role="alert"><h3>Graph view unavailable</h3><p>Your answer and evidence are still available. Use the accessible graph list below to select {linkName}.</p></div>}>
            <Suspense fallback={<div className="graph-empty" role="status"><p>Preparing graph controls…</p></div>}>
              <GraphCanvas graph={graph} selection={selection} onSelect={onSelect} />
            </Suspense>
          </ErrorBoundary>
        ) : (
          <div className="graph-empty">
            <div className="empty-network" aria-hidden="true"><i /><i /><i /><i /><span /></div>
            <span className="eyebrow">{loading ? 'Reading the sources' : 'A view of cited passages only'}</span>
            <h3>{loading ? 'Tracing the connections…' : graph ? 'No graph relationships returned' : 'Ideas connect here.'}</h3>
            <p>{graph ? 'An answer may still be useful without a graph. Read the narrative, passages, and notices for context.' : phraseGraph ? 'Ask a question to map its cited sources and passages, with any case-insensitive exact phrase matches. This is not a map of the full corpus.' : 'Ask a question to explore a graph scoped only to the passages cited in its response.'}</p>
            <span className="empty-hint">Pan · Zoom · Select · Explore evidence</span>
          </div>
        )}
        {loading && hasNodes && <div className="graph-loading" role="status">Previous graph · new question in progress</div>}
      </div>
      <div className="graph-footer">
        <div className="legend" aria-label="Node types">
          {types.size ? [...types].map(([type, count]) => (
            <span key={type}><i className={`type-dot type-${colorType(type)}`} />{type} <b>{count}</b></span>
          )) : <span>Node types and counts appear with real results.</span>}
        </div>
        <span>Up to {edgeLimit} {linkName} per response · not a corpus-wide graph</span>
      </div>
      {selection && <div className="graph-selection"><span role="status">{selection.kind === 'citation' ? 'Citation' : selection.kind === 'edge' ? 'Relationship' : 'Node'} selected</span><button type="button" className="text-button" onClick={onViewEvidence}>View highlighted passages · {selectedEvidenceCount} <span aria-hidden="true">↗</span></button></div>}
      {graph?.truncated && <p className="notice" role="status">{phraseGraph ? 'Graph capped: only some cited-source and case-insensitive exact phrase-match links are shown.' : 'Graph capped: only part of the reported relationships is shown.'} Narrow your question for a more focused view.</p>}
      {graph && !graph.edges.length && <p className="notice">{phraseGraph ? 'No source or case-insensitive exact phrase-match links were returned. This is not evidence that topics or relationships are absent from the corpus.' : 'No relationships were returned for this answer. This does not mean that no relationships exist in the speeches.'}</p>}
      {!!graph?.nodes.length && (
        <details className="graph-data">
          <summary>Explore graph as an accessible list <span>({graph.edges.length} {linkName})</span></summary>
          <p>{phraseGraph ? 'These links show canonical source containment and case-insensitive, whole-word literal curated phrase matches, not inferred semantic facts.' : 'These are reported assertions, not independently verified facts.'} Select an item to highlight its evidence.</p>
          <h3>Nodes</h3>
          <ul className="node-list">{graph.nodes.map(node => (
            <li key={node.id}><button type="button" aria-pressed={selection?.kind === 'node' && selection.id === node.id} onClick={() => onSelect({ kind: 'node', id: node.id })}>{node.label} <span>· {node.type}</span></button></li>
          ))}</ul>
          <h3>{phraseGraph ? 'Source & literal phrase links' : 'Reported relationships'}</h3>
          <ul className="relationship-list">{graph.edges.map(edge => (
            <li key={edge.id}>
              <button type="button" aria-label={`${nodesById.get(edge.source)?.label ?? edge.source} → ${edge.label} → ${nodesById.get(edge.target)?.label ?? edge.target}; ${edge.evidence_ids.length} evidence references`} aria-pressed={selection?.kind === 'edge' && selection.id === edge.id} onClick={() => onSelect({ kind: 'edge', id: edge.id })}>
                <strong>{nodesById.get(edge.source)?.label ?? edge.source}</strong>
                <span> → {edge.label} → </span>
                <strong>{nodesById.get(edge.target)?.label ?? edge.target}</strong>
                <small>{edge.evidence_ids.length} evidence reference{edge.evidence_ids.length === 1 ? '' : 's'}</small>
              </button>
            </li>
          ))}</ul>
        </details>
      )}
    </section>
  );
}

export function colorType(type: string): string {
  const normalized = type.toLowerCase();
  if (normalized.includes('speech')) return 'speech';
  if (normalized.includes('passage')) return 'passage';
  if (normalized.includes('topic') || normalized.includes('theme')) return 'topic';
  if (normalized.includes('person')) return 'person';
  if (normalized.includes('org')) return 'org';
  if (normalized.includes('place') || normalized.includes('location')) return 'place';
  return 'date';
}
