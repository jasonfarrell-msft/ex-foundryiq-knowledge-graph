import { useId, useState } from 'react';
import { canonicalSummary, coverageGuidance, coverageSummary, formatCount } from './coverage';
import type { Catalog } from './types';

export const CATALOG_PAGE_SIZE = 25;

function searchable(value: string): string {
  return value.normalize('NFKD').replace(/\p{M}/gu, '').toLocaleLowerCase('en-US');
}

export function CatalogBrowser({ catalog }: { catalog: Catalog }) {
  const [query, setQuery] = useState('');
  const [page, setPage] = useState(0);
  const inputId = useId();
  const tokens = searchable(query).trim().split(/\s+/).filter(Boolean);
  // Filter the entire catalog in memory, not just the current page. Rendering
  // remains bounded; no extra API requests are made for searches or pagination.
  const matches = catalog.speeches.filter(speech => {
    const text = searchable([speech.title, speech.speaker, speech.date].filter(Boolean).join(' '));
    return tokens.every(token => text.includes(token));
  });
  const pageCount = Math.max(1, Math.ceil(matches.length / CATALOG_PAGE_SIZE));
  const currentPage = Math.min(page, pageCount - 1);
  const start = currentPage * CATALOG_PAGE_SIZE;
  const visible = matches.slice(start, start + CATALOG_PAGE_SIZE);
  const canonical = canonicalSummary(catalog.coverage);

  return (
    <div className="catalog-browser">
      <p className="catalog-coverage">{coverageSummary(catalog.coverage)}{canonical && ` · ${canonical}`}</p>
      <p className="muted">{coverageGuidance(catalog.coverage)}</p>
      <div className="catalog-search">
        <label htmlFor={inputId}>Find a speech by title, speaker, or date</label>
        <div>
          <input
            type="search"
            id={inputId}
            value={query}
            placeholder="Try Lincoln, Gettysburg, or 1863"
            aria-describedby={`${inputId}-summary`}
            onChange={event => { setQuery(event.target.value); setPage(0); }}
          />
          {query && <button className="text-button" type="button" onClick={() => { setQuery(''); setPage(0); }}>Clear filter</button>}
        </div>
      </div>
      <p className="catalog-summary" id={`${inputId}-summary`} role="status">
        {matches.length
          ? `Showing ${formatCount(start + 1)}–${formatCount(start + visible.length)} of ${formatCount(matches.length)} matching speeches`
          : 'No speeches match this filter'}
        {` · ${formatCount(catalog.speeches.length)} catalog records total · ${CATALOG_PAGE_SIZE} per page.`}
      </p>
      <ul className="catalog-records" aria-label="Speech catalog results">
        {visible.map(speech => <li key={speech.id}><strong>{speech.title}</strong><span>{speech.speaker ?? 'Speaker not supplied'} · {speech.date ?? 'Date not supplied'}</span></li>)}
      </ul>
      {!matches.length && <p className="catalog-no-matches">Try a shorter title, another speaker, or a date such as a year. No catalog records have been removed.</p>}
      <nav className="catalog-pagination" aria-label="Speech catalog pages">
        <button type="button" disabled={currentPage === 0} onClick={() => setPage(currentPage - 1)}>Previous page</button>
        <span>Page {formatCount(currentPage + 1)} of {formatCount(pageCount)}</span>
        <button type="button" disabled={currentPage === pageCount - 1} onClick={() => setPage(currentPage + 1)}>Next page</button>
      </nav>
      {catalog.speeches.length !== catalog.coverage.source_count && (
        <p className="notice" role="status">Catalog count differs from reported coverage: {formatCount(catalog.speeches.length)} records listed, {formatCount(catalog.coverage.source_count)} sources reported. This catalog may be incomplete.</p>
      )}
    </div>
  );
}
