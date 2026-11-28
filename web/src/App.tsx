import { Activity, useEffect, useRef, useState, type FormEvent } from 'react';
import { ApiError, askQuestion, getCatalog, getHealth } from './api';
import { Answer } from './Answer';
import { Evidence } from './Evidence';
import { Graph } from './Graph';
import { CatalogBrowser } from './CatalogBrowser';
import { canonicalSummary, coverageGuidance, coverageSummary, formatCount, isPhraseGraph } from './coverage';
import type { AskResult, Catalog, Health, RetrievalMode, Selection } from './types';

const suggestions = [
  'How does Roosevelt’s March 4, 1933 inaugural address connect banking reform, agriculture, and employment?',
  'Compare Roosevelt’s 1933 and Obama’s 2009 inaugural addresses on economic recovery.',
  'What themes do Kennedy’s 1961 and Reagan’s 1981 inaugural addresses share?',
];
const suggestionLabels = ['Roosevelt & economic reform', 'Two eras of recovery', 'Kennedy meets Reagan'];
const fullSuggestions = [
  'Compare Lincoln’s Gettysburg Address and Washington’s Farewell Address on national unity.',
  'How does Washington’s Farewell Address discuss political parties and foreign alliances?',
  'Compare Roosevelt’s 1933 and Obama’s 2009 inaugural addresses on economic recovery.',
];
const fullSuggestionLabels = ['Lincoln & Washington', 'Washington on alliances', 'Two eras of recovery'];
type Tab = 'answer' | 'graph' | 'evidence';
interface FailedRequest { question: string; error: ApiError }

function ErrorNotice({ error, prefix }: { error: ApiError; prefix?: string }) {
  return <div className="error-notice" role="alert">
    {prefix && <strong>{prefix}</strong>}
    <p>{error.message}</p>
    {error.status === 429 && error.retryAfter && <p>Retry-After: {error.retryAfter}. No automatic retry will be made.</p>}
  </div>;
}

function asError(error: unknown): ApiError {
  return error instanceof ApiError ? error : new ApiError('An unexpected error occurred. Please try again when ready.');
}

export function App() {
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState<ApiError | null>(null);
  const [catalog, setCatalog] = useState<Catalog | null>(null);
  const [catalogError, setCatalogError] = useState<ApiError | null>(null);
  const [catalogOpen, setCatalogOpen] = useState(false);
  const [question, setQuestion] = useState('');
  const [mode, setMode] = useState<RetrievalMode>('rag');
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const [result, setResult] = useState<AskResult | null>(null);
  const [history, setHistory] = useState<AskResult[]>([]);
  const [failure, setFailure] = useState<FailedRequest | null>(null);
  const [cancelled, setCancelled] = useState(false);
  const [selection, setSelection] = useState<Selection>(null);
  const [tab, setTab] = useState<Tab>('answer');
  const [azureResponded, setAzureResponded] = useState(false);
  const activeRequest = useRef<AbortController | null>(null);
  const textarea = useRef<HTMLTextAreaElement>(null);
  const answerHeading = useRef<HTMLHeadingElement>(null);
  const pending = pendingQuestion !== null;
  const limit = Math.max(1, Math.min(2000, health?.limits.question_characters ?? 2000));
  const edgeLimit = Math.min(60, health?.limits.graph_edges ?? 60);
  // Current collection coverage must not be overwritten by a historical result.
  const coverage = health?.coverage ?? catalog?.coverage ?? result?.coverage;
  const canonical = canonicalSummary(coverage);
  const phraseGraph = isPhraseGraph(result?.graph ?? null, result?.coverage ?? coverage);
  const currentSuggestions = coverage?.full_corpus_available ? fullSuggestions : suggestions;
  const currentSuggestionLabels = coverage?.full_corpus_available ? fullSuggestionLabels : suggestionLabels;

  useEffect(() => {
    const controller = new AbortController();
    // Readiness and catalog only. Questions are never sent from an effect.
    getHealth(controller.signal).then(setHealth).catch(error => {
      if (!controller.signal.aborted) setHealthError(asError(error));
    });
    getCatalog(controller.signal).then(setCatalog).catch(error => {
      if (!controller.signal.aborted) setCatalogError(asError(error));
    });
    return () => {
      controller.abort();
      activeRequest.current?.abort();
    };
  }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const submitted = question.trim();
    // Ref guard also prevents duplicate submissions before the next render.
    if (!submitted || submitted.length > limit || activeRequest.current) return;
    const controller = new AbortController();
    activeRequest.current = controller;
    setPendingQuestion(submitted);
    setFailure(null);
    setCancelled(false);
    try {
      const next = await askQuestion(submitted, controller.signal, health?.graph_rag_available ? mode : undefined);
      if (controller.signal.aborted) return;
      setResult(next);
      setHistory(previous => [next, ...previous].slice(0, 12));
      setSelection(null);
      setTab('answer');
      setAzureResponded(true);
      // Wait until the new answer is rendered before moving keyboard focus.
      requestAnimationFrame(() => answerHeading.current?.focus({ preventScroll: true }));
    } catch (error) {
      if (controller.signal.aborted) setCancelled(true);
      else setFailure({ question: submitted, error: asError(error) });
    } finally {
      if (activeRequest.current === controller) {
        activeRequest.current = null;
        setPendingQuestion(null);
      }
    }
  }

  function chooseCitation(id: string) {
    setTab('evidence');
    setSelection({ kind: 'citation', id });
  }

  function showHistory(item: AskResult) {
    if (pending) return;
    setResult(item);
    setQuestion(item.question);
    if (item.mode) setMode(item.mode);
    setSelection(null);
    setFailure(null);
    setCancelled(false);
    setTab('answer');
  }

  const selectedIds = new Set<string>();
  if (selection?.kind === 'citation') selectedIds.add(selection.id);
  else if (selection && result) {
    for (const edge of result.graph.edges) {
      const match = selection.kind === 'edge'
        ? edge.id === selection.id
        : edge.source === selection.id || edge.target === selection.id;
      if (match) for (const id of edge.evidence_ids) {
        if (result.citations.some(citation => citation.id === id)) selectedIds.add(id);
      }
    }
  }
  const warnings = [...new Set([...(health?.warnings ?? []), ...(catalog?.warnings ?? []), ...(result?.warnings ?? [])])];

  return (
    <>
      <a className="skip-link" href="#question">Skip to question</a>
      <header className="site-header">
        <a className="brand" href="/" aria-label="Speech Atlas home"><span className="brand-mark" aria-hidden="true">sa<span>↗</span></span><span>Speech Atlas<small>PRESIDENTIAL SPEECHES</small></span></a>
        <div className="header-status">
          <span className="sample-badge">{coverageSummary(coverage)}</span>
          <span className={`connection-badge ${azureResponded || (health?.configured && health.status === 'ok') ? 'ready' : ''}`}>
            <i aria-hidden="true" />{azureResponded ? 'Azure · responded this session' : health?.status === 'ok' && health.configured ? 'API ready · Azure not verified' : healthError ? 'API unavailable' : health ? 'API configuration needed' : 'Checking API readiness'}
          </span>
        </div>
      </header>
      <div className="app-shell">
        <aside className="sidebar" aria-label="Research workspace">
          <div className="workspace-label">YOUR WORKSPACE <span aria-hidden="true">↗</span></div>
          <div className="rail-current"><span aria-hidden="true">▤</span> Speech explorer <span className="rail-dot" /></div>
          <button type="button" className={`rail-button ${catalogOpen ? 'active' : ''}`} onClick={() => setCatalogOpen(!catalogOpen)} aria-expanded={catalogOpen} aria-controls="speech-catalog"><span aria-hidden="true">▦</span> Speech collection <span>{catalog ? formatCount(catalog.speeches.length) : '—'}</span></button>
          <div className="history-heading"><span>THIS SESSION</span><span>{history.length.toString().padStart(2, '0')}</span></div>
          {history.length ? <ol className="history-list">{history.map((item, index) => (
            <li key={`${item.request_id}-${index}`}><button type="button" disabled={pending} onClick={() => showHistory(item)} aria-current={result === item ? 'true' : undefined}><span aria-hidden="true">↳</span>{item.question}{item.partial && <small>Partial response</small>}</button></li>
          ))}</ol> : <p className="history-empty">Your questions will find a home here.<br /><span>Kept in memory for this session only.</span></p>}
          <div className="collection-note"><span className="eyebrow">Collection coverage</span><h2>{coverage?.label ?? 'A collection in context.'}</h2><p>{coverageSummary(coverage)}{canonical && ` · ${canonical}`}</p><div className={`corpus-warning ${coverage?.full_corpus_available ? 'corpus-available' : ''}`}><span aria-hidden="true">◐</span> {coverage ? coverage.full_corpus_available ? 'Corpus coverage ≠ answer coverage' : 'Limited sample coverage' : 'Awaiting coverage information'}</div><small>{coverageGuidance(coverage)}</small></div>
        </aside>
        <main id="main-content">
          <section className="intro" aria-labelledby="page-heading">
            <div className="intro-kicker"><span className="eyebrow">THE PRESIDENTIAL SPEECH EXPLORER</span><span className="edition">RESEARCH / 01</span></div>
            <h1 id="page-heading">Great speeches.<br className="mobile-break" /> Connected ideas.</h1>
            <p>Ask a question. Read the sources. See how the ideas connect.</p>
            <p className={`scope-note ${coverage?.full_corpus_available ? 'corpus-available' : ''}`}><span aria-hidden="true">◐</span> {coverageSummary(coverage)}{canonical && ` · ${canonical}`}</p>
            <p className="retrieval-scope">{coverageGuidance(coverage)}</p>
          </section>
          <Activity mode={catalogOpen ? 'visible' : 'hidden'}>
            <section id="speech-catalog" className="catalog" aria-labelledby="catalog-heading">
              <div className="panel-heading"><div><span className="eyebrow">COLLECTION COVERAGE</span><h2 id="catalog-heading">The speech collection</h2></div><button type="button" className="text-button" onClick={() => setCatalogOpen(false)}>Close collection</button></div>
              {catalogError ? <ErrorNotice error={catalogError} prefix="Collection unavailable" /> : catalog ? <CatalogBrowser catalog={catalog} /> : <p role="status">Loading the speech collection…</p>}
            </section>
          </Activity>
          {healthError && <ErrorNotice error={healthError} prefix="API readiness could not be checked" />}
          {health && (!health.configured || health.status !== 'ok') && <div className="notice" role="status">API configuration is incomplete. Ask your administrator to check the linked speech service. Azure connectivity has not been verified.</div>}
          <section className="question-card" aria-labelledby="question-heading">
            <div className="question-topline"><label id="question-heading" htmlFor="question">What would you like to explore?</label><span>01 / ASK THE COLLECTION</span></div>
            {health?.graph_rag_available && (
              <div className="mode-toggle" role="group" aria-label="Retrieval mode">
                <span className="mode-label">RETRIEVAL MODE</span>
                <div className="mode-buttons">
                  <button type="button" disabled={pending} aria-pressed={mode === 'rag'} onClick={() => setMode('rag')}>Standard RAG</button>
                  <button type="button" disabled={pending} aria-pressed={mode === 'graph'} onClick={() => setMode('graph')}>Graph RAG (GQL)</button>
                </div>
              </div>
            )}
            <form onSubmit={submit}>
              <textarea ref={textarea} id="question" name="question" rows={3} maxLength={limit} value={question} onChange={event => setQuestion(event.target.value)} disabled={pending} placeholder="How did presidents frame the challenges of their time?" aria-describedby="question-help question-count" />
              <div className="question-actions">
                <p id="question-help">{coverage ? coverage.full_corpus_available ? 'Retrieves a subset of the full corpus.' : 'Retrieval is limited to this sample.' : 'Coverage not yet confirmed.'}<span id="question-count"> {question.length.toLocaleString()} / {limit.toLocaleString()}</span></p>
                <div className="submit-actions">{pending && <button className="text-button" type="button" onClick={() => activeRequest.current?.abort()}>Cancel request</button>}<button type="submit" className="ask-button" disabled={pending || !question.trim() || question.trim().length > limit}>{pending ? <><span className="spinner" aria-hidden="true" /> Exploring…</> : <>Ask the collection <span aria-hidden="true">↗</span></>}</button></div>
              </div>
            </form>
            <p className="muted">Questions may incur Azure Search and model charges. No automatic retries.</p>
            <div className="suggestions"><span>TRY A QUESTION</span>{currentSuggestions.map((suggestion, index) => <button key={suggestion} type="button" disabled={pending} onClick={() => { setQuestion(suggestion); textarea.current?.focus(); }} title={suggestion}>{currentSuggestionLabels[index]} <span aria-hidden="true">↗</span></button>)}</div>
          </section>
          <div className="request-state" aria-live="polite" role="status">
            {pending ? <p><span className="spinner" aria-hidden="true" /> Reading cited passages for <strong>“{pendingQuestion}”</strong>. {result && 'The previous result remains below.'} This can take up to 42 seconds.</p> : cancelled ? <p>Request cancelled in this browser. {result && 'The previous result is still shown.'} Cancellation may not stop server processing or charges.</p> : !result && !failure ? <p><span aria-hidden="true">↳</span> Start with a question. Every connection begins with a source.</p> : null}
          </div>
          {failure && <div className="failure-block"><p className="failed-question">Request failed for: <strong>“{failure.question}”</strong></p><ErrorNotice error={failure.error} />{result && <p className="notice">Previous result retained below for “{result.question}”. It is not a response to the failed question.</p>}</div>}
          {result && <div className="result-context"><span className="eyebrow">{pending || failure || cancelled ? 'PREVIOUS RESULT' : 'SHOWING RESULT FOR'}</span><p>{result.question}</p><small>{result.mode === 'graph' ? 'Graph RAG (GQL) · ' : result.mode === 'rag' ? 'Standard RAG · ' : ''}Response collection: {result.coverage.label} · {formatCount(result.coverage.source_count)} available sources{canonicalSummary(result.coverage) && ` · ${canonicalSummary(result.coverage)}`} · {Number.isFinite(result.timing_ms) ? `${(result.timing_ms / 1000).toFixed(1)}s` : 'Timing unavailable'}{result.partial ? ' · Partial response' : ''}</small><p className="result-scope">{result.citations.length} cited passages in this response; this is not an exhaustive reading of the collection.</p></div>}
          {result?.partial && <div className="notice partial-notice" role="status"><strong>Partial response.</strong> Some answer, graph, or evidence data may be missing. Review the service notices; this is not a complete finding.</div>}
          {!!warnings.length && <div className="service-notices" role="status"><strong>Collection & service notices</strong><ul>{warnings.map(warning => <li key={warning}>{warning}</li>)}</ul></div>}
          <div className="result-tabs" role="tablist" aria-label="Research result views">
            {(['answer', 'graph', 'evidence'] as const).map(item => <button key={item} type="button" role="tab" id={`tab-${item}`} aria-controls={`view-${item}`} aria-selected={tab === item} tabIndex={tab === item ? 0 : -1} onClick={() => setTab(item)} onKeyDown={event => {
              const items: Tab[] = ['answer', 'graph', 'evidence'];
              const index = items.indexOf(item);
              const next = event.key === 'ArrowRight' ? items[(index + 1) % 3] : event.key === 'ArrowLeft' ? items[(index + 2) % 3] : event.key === 'Home' ? 'answer' : event.key === 'End' ? 'evidence' : null;
              if (next) { event.preventDefault(); setTab(next); document.getElementById(`tab-${next}`)?.focus(); }
            }}>{item === 'answer' ? 'Answer' : item === 'graph' ? 'Graph' : `Evidence${result ? ` (${result.citations.length})` : ''}`}</button>)}
          </div>
          <div className={`results-grid tabbed-results ${result ? 'has-result' : ''}`}>
            <Activity mode={tab === 'answer' ? 'visible' : 'hidden'}>
              <div className="answer-region" id="view-answer" role="tabpanel" aria-labelledby="tab-answer">
                <section className="answer-panel" aria-labelledby="answer-heading">
                  <div className="panel-heading"><div><span className="eyebrow">01 / Read the synthesis</span><h2 id="answer-heading" ref={answerHeading} tabIndex={-1}>The answer</h2></div><span className="answer-icon" aria-hidden="true">✳</span></div>
                  {result ? <><Answer answer={result.answer} citations={result.citations} onCitation={chooseCitation} /><div className="answer-footnote">AI-generated synthesis · Check claims against the cited passages.</div><details className="request-details"><summary>Response metadata</summary><dl className="source-metadata"><div><dt>Request</dt><dd>{result.request_id}</dd></div><div><dt>Mode</dt><dd>{result.mode === 'graph' ? 'Graph RAG (search.graph GQL)' : 'Standard RAG (searchIndex)'}</dd></div><div><dt>Graph scope</dt><dd>{result.graph.basis === 'foundry_graph_index' ? 'Azure AI Search graph index (hydrated passages)' : 'Cited passages only'}</dd></div>{!!result.gql_queries?.length && <div><dt>GQL</dt><dd>{result.gql_queries.map((q, idx) => <code key={idx} className="gql-snippet">{q}</code>)}</dd></div>}</dl></details></> : <div className="answer-empty"><span className="editorial-line" /><h3>A little context.<br />A clearer perspective.</h3><p>Your answer will appear here with clickable references to the original passages.</p><p className="muted">No question has been sent.</p></div>}
                </section>
              </div>
            </Activity>
            <Activity mode={tab === 'graph' ? 'visible' : 'hidden'}>
              <div className="graph-region" id="view-graph" role="tabpanel" aria-labelledby="tab-graph"><Graph graph={result?.graph ?? null} coverage={result?.coverage ?? coverage} selection={selection} onSelect={setSelection} loading={pending} edgeLimit={edgeLimit} selectedEvidenceCount={selectedIds.size} onViewEvidence={() => { setTab('evidence'); requestAnimationFrame(() => document.getElementById('evidence-heading')?.focus()); }} /></div>
            </Activity>
            <Activity mode={tab === 'evidence' ? 'visible' : 'hidden'}>
              <div className="evidence-region" id="view-evidence" role="tabpanel" aria-labelledby="tab-evidence"><Evidence citations={result?.citations ?? []} selectedIds={selectedIds} selection={selection} onSelect={chooseCitation} onClear={() => setSelection(null)} /></div>
            </Activity>
          </div>
          <footer className="site-footer"><span>Speech Atlas <span aria-hidden="true">/</span> Words in context.</span><span>{coverageSummary(coverage)} · {phraseGraph ? 'Graph: cited sources, passages & case-insensitive exact phrase matches only' : result ? 'Graph: cited-passage assertions only' : 'Graph scope: cited passages only'}</span></footer>
        </main>
      </div>
    </>
  );
}
