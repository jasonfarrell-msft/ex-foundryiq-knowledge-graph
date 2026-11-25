import { useEffect, useRef } from 'react';
import type { Citation, Selection } from './types';

interface EvidenceProps {
  citations: Citation[];
  selectedIds: Set<string>;
  selection: Selection;
  onSelect: (id: string) => void;
  onClear: () => void;
}

export function Evidence({ citations, selectedIds, selection, onSelect, onClear }: EvidenceProps) {
  const passages = useRef(new Map<string, HTMLElement>());
  useEffect(() => {
    if (selection?.kind === 'citation') {
      const passage = passages.current.get(selection.id);
      passage?.focus({ preventScroll: true });
      passage?.scrollIntoView?.({ behavior: 'smooth', block: 'nearest' });
    }
  }, [selection]);

  return (
    <section className="evidence-panel" aria-labelledby="evidence-heading">
      <div className="panel-heading">
        <div><span className="eyebrow">03 / The source material</span><h2 id="evidence-heading" tabIndex={-1}>Cited passages <span className="count">{citations.length}</span></h2></div>
      </div>
      <p className="panel-intro">Read the words behind the answer. Select a relationship to highlight its supporting passages.</p>
      <p className="source-projection-note">Passages are readable text projections of source HTML, not raw-byte copies. Source and passage identifiers are retained below.</p>
      {selection && (
        <div className="selection-summary" role="status">
          <span>{selectedIds.size} passage{selectedIds.size === 1 ? '' : 's'} highlighted{selection.kind !== 'citation' ? ` for selected ${selection.kind}` : ''}.</span>
          <button type="button" className="text-button" onClick={onClear}>Clear selection</button>
        </div>
      )}
      {!citations.length ? (
        <div className="small-empty"><span aria-hidden="true">“ ”</span><h3>No passages yet</h3><p>Source excerpts will appear here after a question returns cited evidence.</p></div>
      ) : (
        <ol className="passage-list">
          {citations.map((citation, index) => {
            const selected = selectedIds.has(citation.id);
            return (
              <li key={citation.id}>
                <article
                  className={`passage ${selected ? 'is-selected' : ''}`}
                  tabIndex={-1}
                  aria-label={`Passage ${index + 1}: ${citation.title}${selected ? ', highlighted' : ''}`}
                  ref={element => {
                    if (element) passages.current.set(citation.id, element);
                    return () => { passages.current.delete(citation.id); };
                  }}
                >
                  <div className="passage-top"><span className="passage-number">{String(index + 1).padStart(2, '0')}</span>{selected && <span className="selected-label">✓ Selected</span>}</div>
                  <h3><button className="passage-title" type="button" aria-pressed={selected} onClick={() => onSelect(citation.id)}>{citation.title}</button></h3>
                  <p className="source-byline">{citation.speaker ?? 'Speaker not supplied'} · {citation.date ?? 'Date not supplied'}</p>
                  <blockquote>“{citation.text}”</blockquote>
                  <dl className="source-metadata">
                    <div><dt>Source</dt><dd>{citation.source_id ?? 'Not supplied'}</dd></div>
                    {citation.canonical_id && <div><dt>Canonical</dt><dd>{citation.canonical_id}</dd></div>}
                    <div><dt>Passage</dt><dd>{citation.chunk_id}</dd></div>
                    <div><dt>Citation</dt><dd>{citation.id}</dd></div>
                  </dl>
                </article>
              </li>
            );
          })}
        </ol>
      )}
    </section>
  );
}
