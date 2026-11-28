import { act, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from './App';
import { catalog, fullHealth, health, json, makeFullCatalog, makeFullResult, makeResult } from './test/fixtures';
import type { AskResult, Selection } from './types';

// Canvas layout needs browser geometry. Keep real graph lists and evidence UI,
// and exercise the same opaque-ID callbacks as the canvas.
vi.mock('./GraphCanvas', () => ({
  default: ({ graph, onSelect }: { graph: AskResult['graph']; onSelect: (selection: Selection) => void }) => <div data-testid="flow">
    {graph.nodes.map(node => <button key={node.id} onClick={() => onSelect({ kind: 'node', id: node.id })}>Canvas node {node.id}</button>)}
    {graph.edges.map(edge => <button key={edge.id} onClick={() => onSelect({ kind: 'edge', id: edge.id })}>Canvas edge {edge.id}</button>)}
  </div>,
}));

function installFetch(ask: () => Promise<Response> = async () => json(makeResult()), overrides: { health?: Response; catalog?: Response } = {}) {
  const fetch = vi.fn().mockImplementation((url: string) => {
    if (url === '/api/health') return Promise.resolve(overrides.health ?? json(health));
    if (url === '/api/catalog') return Promise.resolve(overrides.catalog ?? json(catalog));
    if (url === '/api/ask') return ask();
    throw new Error(`Unexpected request: ${url}`);
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

async function submit(question = 'How are the test topics connected?') {
  const user = userEvent.setup();
  const textbox = screen.getByRole('textbox', { name: /What would you like to explore/ });
  await user.clear(textbox);
  await user.type(textbox, question);
  await user.click(screen.getByRole('button', { name: /Ask the collection/ }));
  return user;
}

async function viewTab(name: 'Answer' | 'Graph' | 'Evidence') {
  await userEvent.click(screen.getByRole('tab', { name: new RegExp(`^${name}`) }));
}

beforeEach(() => {
  vi.mocked(window.matchMedia).mockImplementation((query: string) => ({ matches: false, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }));
  vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { callback(0); return 0; });
});

describe('research workspace', () => {
  it('starts empty; readiness is not cloud connectivity; suggestions fill only', async () => {
    const fetch = installFetch();
    render(<App />);
    expect(await screen.findByText('API ready · Azure not verified')).toBeInTheDocument();
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'view-answer');
    await viewTab('Graph');
    expect(screen.getByText('Ideas connect here.')).toBeInTheDocument();
    expect(screen.queryByTestId('flow')).not.toBeInTheDocument();
    expect(screen.getByText('No question has been sent.')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /Roosevelt & economic reform/ }));
    expect(screen.getByRole('textbox')).toHaveValue('How does Roosevelt’s March 4, 1933 inaugural address connect banking reform, agriculture, and employment?');
    expect(screen.getByRole('textbox')).toHaveAttribute('maxlength', '2000');
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/health', '/api/catalog']);
  });

  it('shows answer, actual graph IDs, metadata and safely quoted passages', async () => {
    installFetch();
    const { container } = render(<App />);
    await submit();
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
    await viewTab('Graph');
    expect(await screen.findByTestId('flow')).toBeInTheDocument();
    expect(screen.getByText('Azure · responded this session')).toBeInTheDocument();
    expect(screen.getByText('Graph relationships in cited passages; not proof of every answer claim.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Canvas edge edge:opaque:1' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Canvas edge edge:opaque:2' })).toBeInTheDocument();
    await viewTab('Evidence');
    expect(screen.getByText('opaque/chunk:1')).toBeInTheDocument();
    expect(screen.getByText('test-source-1')).toBeInTheDocument();
    expect(screen.getByText(/<script>not executable/)).toBeInTheDocument();
    expect(container.querySelector('script')).toBeNull();
  });

  it('selects citation, edge-specific evidence and incident-node evidence', async () => {
    installFetch();
    render(<App />);
    const user = await submit();
    const marker = await screen.findByRole('button', { name: /Read passage 1/ });
    await user.click(marker);
    const first = screen.getByRole('article', { name: /Passage 1/ });
    const second = screen.getByRole('article', { name: /Passage 2/ });
    expect(first).toHaveClass('is-selected');
    expect(first).toHaveFocus();
    expect(second).not.toHaveClass('is-selected');
    await viewTab('Graph');
    await user.click(await screen.findByRole('button', { name: 'Canvas edge edge:opaque:2' }));
    expect(first).not.toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    await user.click(screen.getByRole('button', { name: 'Canvas node node:opaque:1' }));
    expect(first).toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    expect(screen.getByText('2 passages highlighted for selected node.')).toBeInTheDocument();
    await viewTab('Evidence');
    await user.click(screen.getByRole('button', { name: 'Clear selection' }));
    expect(first).not.toHaveClass('is-selected');
    expect(second).not.toHaveClass('is-selected');
  });

  it('provides keyboard-accessible graph list selection, retaining duplicate assertions', async () => {
    installFetch();
    render(<App />);
    const user = await submit();
    await viewTab('Graph');
    await screen.findByTestId('flow');
    await user.click(screen.getByText(/Explore graph as an accessible list/));
    const relationshipButtons = screen.getAllByRole('button', { name: /Reform → reported alongside → Employment/ });
    expect(relationshipButtons).toHaveLength(2);
    relationshipButtons[1].focus();
    await user.keyboard('{Enter}');
    await viewTab('Evidence');
    expect(screen.getByRole('article', { name: /Passage 2/ })).toHaveClass('is-selected');
  });

  it('keeps partial answers and empty graph explicit', async () => {
    installFetch(async () => json(makeResult({ partial: true, warnings: ['Retrieval was incomplete.'], graph: { scope: 'cited_passages', nodes: [], edges: [], truncated: true } })));
    render(<App />);
    await submit();
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
    expect(screen.getByText('Partial response.')).toBeInTheDocument();
    expect(screen.getByText('Retrieval was incomplete.')).toBeInTheDocument();
    await viewTab('Graph');
    expect(screen.getByText('No graph relationships returned')).toBeInTheDocument();
    expect(screen.getByText(/Graph capped:/)).toBeInTheDocument();
    await viewTab('Evidence');
    expect(screen.getByRole('article', { name: /Passage 1/ })).toBeInTheDocument();
  });

  it('keeps a populated partial graph and its answer visible', async () => {
    installFetch(async () => json(makeResult({ partial: true })));
    render(<App />);
    await submit();
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
    await viewTab('Graph');
    expect(await screen.findByTestId('flow')).toBeInTheDocument();
    expect(screen.getByText('Partial response.')).toBeInTheDocument();
  });

  it('prevents double submission and labels the in-flight question', async () => {
    let resolve!: (response: Response) => void;
    const fetch = installFetch(() => new Promise<Response>(done => { resolve = done; }));
    const { container } = render(<App />);
    await submit();
    expect(screen.getByRole('button', { name: /Exploring/ })).toBeDisabled();
    expect(screen.getByRole('textbox')).toBeDisabled();
    fireEvent.submit(container.querySelector('form')!);
    fireEvent.submit(container.querySelector('form')!);
    expect(fetch.mock.calls.filter(call => call[0] === '/api/ask')).toHaveLength(1);
    expect(screen.getByText(/Reading cited passages for/)).toBeInTheDocument();
    await act(async () => resolve(json(makeResult())));
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
  });

  it('cancels the browser request without a billing promise or automatic retry', async () => {
    let requestSignal: AbortSignal | undefined;
    const fetch = vi.fn().mockImplementation((url: string, init: RequestInit) => {
      if (url === '/api/health') return Promise.resolve(json(health));
      if (url === '/api/catalog') return Promise.resolve(json(catalog));
      requestSignal = init.signal ?? undefined;
      return new Promise((_, reject) => {
        requestSignal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
      });
    });
    vi.stubGlobal('fetch', fetch);
    render(<App />);
    const user = await submit();
    await user.click(screen.getByRole('button', { name: 'Cancel request' }));
    expect(await screen.findByText(/Cancellation may not stop server processing or charges/)).toBeInTheDocument();
    expect(requestSignal?.aborted).toBe(true);
    expect(fetch.mock.calls.filter(call => call[0] === '/api/ask')).toHaveLength(1);
    expect(screen.getByRole('button', { name: /Ask the collection/ })).toBeEnabled();
  });

  it('preserves previous result on a new failure, with no stale-question ambiguity or retry', async () => {
    const ask = vi.fn().mockResolvedValueOnce(json(makeResult())).mockResolvedValueOnce(json({}, 429, { 'Retry-After': '30' }));
    const fetch = installFetch(ask);
    render(<App />);
    await submit();
    await screen.findByText(/The test passage connects work/);
    await submit('A different question');
    expect(await screen.findByText(/Retry-After: 30/)).toBeInTheDocument();
    expect(screen.getByText(/It is not a response to the failed question/)).toHaveTextContent('How are the test topics connected?');
    expect(screen.getByText(/Request failed for:/)).toHaveTextContent('A different question');
    expect(screen.getByText(/The test passage connects work/)).toBeInTheDocument();
    expect(screen.getByText('PREVIOUS RESULT')).toBeInTheDocument();
    expect(fetch.mock.calls.filter(call => call[0] === '/api/ask')).toHaveLength(2);
  });

  it.each([401, 403])('explains HTTP %s gateway failure without prompting for login', async status => {
    const fetch = installFetch(async () => json({}, status));
    render(<App />);
    await submit();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('No user login is required');
    expect(within(alert).queryByRole('link')).not.toBeInTheDocument();
    expect(fetch.mock.calls.filter(call => call[0] === '/api/ask')).toHaveLength(1);
  });

  it('shows service configuration failure without inventing a response', async () => {
    installFetch(async () => json({}, 503));
    render(<App />);
    await submit();
    expect(await screen.findByRole('alert')).toHaveTextContent('not configured');
    expect(screen.queryByTestId('flow')).not.toBeInTheDocument();
  });

  it('handles a forbidden catalog without blocking the question form', async () => {
    installFetch(undefined, { catalog: json({}, 403) });
    render(<App />);
    await userEvent.click(screen.getByRole('button', { name: /Speech collection/ }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Collection unavailable');
    expect(screen.getByRole('textbox')).toBeEnabled();
  });

  it('uses session history without issuing another request', async () => {
    const fetch = installFetch();
    render(<App />);
    await submit();
    await screen.findByText(/The test passage connects work/);
    await userEvent.click(screen.getByRole('button', { name: 'How are the test topics connected?' }));
    expect(fetch.mock.calls.filter(call => call[0] === '/api/ask')).toHaveLength(1);
    expect(screen.getByRole('textbox')).toHaveValue('How are the test topics connected?');
  });

  it('does not assume sample or full coverage while readiness and catalog are pending', async () => {
    let resolveHealth!: (response: Response) => void;
    let resolveCatalog!: (response: Response) => void;
    const fetch = vi.fn().mockImplementation((url: string) => new Promise<Response>(resolve => {
      if (url === '/api/health') resolveHealth = resolve;
      if (url === '/api/catalog') resolveCatalog = resolve;
    }));
    vi.stubGlobal('fetch', fetch);
    const { container } = render(<App />);
    expect(within(screen.getByRole('banner')).getByText('Collection coverage not yet confirmed')).toBeInTheDocument();
    expect(container).not.toHaveTextContent(/Ten-speech|1,060|1,059|10 sources/);
    await act(async () => { resolveHealth(json(fullHealth)); resolveCatalog(json(makeFullCatalog())); });
    expect(within(screen.getByRole('banner')).getByText('All 1,060 sources indexed')).toBeInTheDocument();
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/health', '/api/catalog']);
  });

  it('shows full coverage separately from retrieval limits and keeps the catalog closed', async () => {
    const fetch = installFetch(undefined, { health: json(fullHealth), catalog: json(makeFullCatalog()) });
    const { container } = render(<App />);
    await screen.findByText('API ready · Azure not verified');
    expect(within(screen.getByRole('banner')).getByText('All 1,060 sources indexed')).toBeInTheDocument();
    expect(screen.getAllByText(/1,059 canonical speeches/).length).toBeGreaterThan(0);
    expect(screen.getAllByText(/Each answer uses a retrieved subset, not every indexed source/).length).toBeGreaterThan(0);
    expect(screen.queryByRole('searchbox')).not.toBeInTheDocument();
    expect(container).not.toHaveTextContent(/Ten-speech sample|ten-speech sample|Full corpus not connected/);
    await userEvent.click(screen.getByRole('button', { name: /Lincoln & Washington/ }));
    expect(screen.getByRole('textbox')).toHaveValue('Compare Lincoln’s Gettysburg Address and Washington’s Farewell Address on national unity.');
    await userEvent.click(screen.getByRole('button', { name: /Speech collection/ }));
    expect(screen.getByRole('searchbox')).toBeInTheDocument();
    expect(within(screen.getByRole('list', { name: 'Speech catalog results' })).getAllByRole('listitem')).toHaveLength(25);
    expect(screen.getByText('Questions may incur Azure Search and model charges. No automatic retries.')).toBeInTheDocument();
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/health', '/api/catalog']);
  });

  it('explains case-insensitive whole-word phrase links and highlights their actual passages', async () => {
    installFetch(async () => json({ ...makeFullResult(), partial: true }), { health: json(fullHealth), catalog: json(makeFullCatalog()) });
    render(<App />);
    const user = await submit();
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
    await viewTab('Graph');
    expect(await screen.findByTestId('flow')).toBeInTheDocument();
    expect(screen.getByText('Partial response.')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Sources & phrase matches' })).toBeInTheDocument();
    expect(screen.getByText(/not a whole-corpus or validated semantic graph/)).toBeInTheDocument();
    expect(screen.getByText(/case-insensitive exact phrase matches only—not/)).toBeInTheDocument();
    await user.click(screen.getByText('What these links mean'));
    expect(screen.getByText(/Canonical speech → Passage means source containment/)).toHaveTextContent('duplicate source records share the canonical Speech node while citations retain source identity');
    expect(screen.getByText(/literal curated phrase matched in that passage/)).toBeInTheDocument();
    expect(screen.getByText(/^Passage → Topic means/)).toHaveTextContent('case-insensitive, whole-word literal');
    expect(screen.getByText(/^Passage → Topic means/)).toHaveTextContent('There is no stemming or fuzzy matching');
    expect(screen.getByText(/Edge labels preserve the original matched case/)).toHaveTextContent('does not mean byte-for-byte identity');
    expect(screen.getByText(/do not establish author intent, semantic relatedness, or agreement/)).toHaveTextContent('assembled separately from answer synthesis');
    expect(screen.getByText(/2 cited passages in this response; this is not an exhaustive reading/)).toBeInTheDocument();
    expect(within(screen.getByLabelText('Node types')).getByText('Passage')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Canvas edge opaque/edge:phrase' }));
    await viewTab('Evidence');
    expect(screen.getByText(/Passages are readable text projections of source HTML, not raw-byte copies/)).toBeInTheDocument();
    expect(screen.getByRole('article', { name: /Passage 1/ })).toHaveClass('is-selected');
    expect(screen.getByRole('article', { name: /Passage 2/ })).not.toHaveClass('is-selected');
    await viewTab('Graph');
    await user.click(screen.getByText(/Explore graph as an accessible list/));
    expect(screen.getByRole('button', { name: /Cited passage → contains phrase "work" → Work/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('shares a canonical Speech node without merging source provenance, passage IDs or reference selection', async () => {
    const response = makeFullResult();
    response.answer = 'Two source records [ref_id:0] [ref_id:duplicate-ref].';
    response.citations = [
      { ...response.citations[0], source_id: 'source-record-a' },
      { ...response.citations[0], id: 'duplicate-ref', source_id: 'source-record-b', chunk_id: 'opaque/chunk:duplicate', text: 'WORK appears in this second synthetic source record.' },
    ];
    response.graph.nodes.push({ id: 'opaque/passage:duplicate', type: 'Passage', label: 'Duplicate source passage' });
    response.graph.edges.push(
      { id: 'edge:duplicate-source', source: 'canonical/speech:1', target: 'opaque/passage:duplicate', label: 'containsPassage', evidence_ids: ['duplicate-ref'] },
      { id: 'edge:duplicate-phrase', source: 'opaque/passage:duplicate', target: 'curated/topic:work', label: 'contains phrase "WORK"', evidence_ids: ['duplicate-ref'] },
    );
    installFetch(async () => json(response), { health: json(fullHealth), catalog: json(makeFullCatalog()) });
    render(<App />);
    const user = await submit();
    await viewTab('Graph');
    await screen.findByTestId('flow');
    expect(screen.getAllByRole('button', { name: 'Canvas node canonical/speech:1' })).toHaveLength(1);
    expect(screen.getByText('4 nodes · 4 links')).toBeInTheDocument();
    await viewTab('Evidence');
    const first = screen.getByRole('article', { name: /Passage 1/ });
    const second = screen.getByRole('article', { name: /Passage 2/ });
    expect(within(first).getByText('source-record-a')).toBeInTheDocument();
    expect(within(second).getByText('source-record-b')).toBeInTheDocument();
    expect(within(first).getByText('opaque/chunk:1')).toBeInTheDocument();
    expect(within(second).getByText('opaque/chunk:duplicate')).toBeInTheDocument();
    for (const passage of [first, second]) {
      expect(within(passage).getByText('Canonical')).toBeInTheDocument();
      expect(within(passage).getByText('canonical/speech:1')).toBeInTheDocument();
    }
    await viewTab('Graph');
    await user.click(screen.getByRole('button', { name: 'Canvas node canonical/speech:1' }));
    expect(first).toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    await user.click(screen.getByRole('button', { name: 'Canvas node opaque/passage:duplicate' }));
    expect(first).not.toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    await user.click(screen.getByText(/Explore graph as an accessible list/));
    await user.click(screen.getByRole('button', { name: /Duplicate source passage → contains phrase "WORK" → Work/ }));
    expect(first).not.toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    await viewTab('Answer');
    await user.click(screen.getByRole('button', { name: /Read passage 1:/ }));
    expect(first).toHaveClass('is-selected');
    expect(second).not.toHaveClass('is-selected');
    await viewTab('Answer');
    await user.click(screen.getByRole('button', { name: /Read passage 2:/ }));
    expect(first).not.toHaveClass('is-selected');
    expect(second).toHaveClass('is-selected');
    expect(within(second).getByText('duplicate-ref')).toBeInTheDocument();
  });

  it('does not replace current full coverage with the coverage of a legacy sample result', async () => {
    installFetch(undefined, { health: json(fullHealth), catalog: json(makeFullCatalog()) });
    render(<App />);
    await submit();
    await viewTab('Graph');
    await screen.findByTestId('flow');
    expect(within(screen.getByRole('banner')).getByText('All 1,060 sources indexed')).toBeInTheDocument();
    expect(screen.getByText(/Response collection: Ten-speech sample · 10 available sources/)).toBeInTheDocument();
    expect(screen.getByText('Graph relationships in cited passages; not proof of every answer claim.')).toBeInTheDocument();
  });

  it.each([false, true])('supports result tabs and citation navigation with mobile=%s', async mobile => {
    vi.mocked(window.matchMedia).mockImplementation((query: string) => ({ matches: mobile, media: query, onchange: null, addListener: vi.fn(), removeListener: vi.fn(), addEventListener: vi.fn(), removeEventListener: vi.fn(), dispatchEvent: vi.fn() }));
    installFetch();
    render(<App />);
    await submit();
    await userEvent.click(await screen.findByRole('button', { name: /Read passage 1/ }));
    expect(screen.getByRole('tab', { name: /Evidence/ })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByRole('article', { name: /Passage 1/ })).toHaveClass('is-selected');
    expect(screen.getByRole('article', { name: /Passage 1/ })).toHaveFocus();
    const graphTab = screen.getByRole('tab', { name: 'Graph' });
    graphTab.focus();
    await userEvent.keyboard('{Home}');
    expect(screen.getByRole('tab', { name: 'Answer' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
    expect(screen.getByRole('tabpanel')).toHaveAttribute('id', 'view-answer');
  });

  it('switches full-width result views without issuing questions', async () => {
    const fetch = installFetch();
    render(<App />);
    await screen.findByText('API ready · Azure not verified');
    for (const tab of ['Graph', 'Evidence', 'Answer'] as const) {
      await viewTab(tab);
      expect(screen.getAllByRole('tabpanel')).toHaveLength(1);
      expect(screen.getByRole('tabpanel')).toHaveAttribute('id', `view-${tab.toLowerCase()}`);
    }
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/health', '/api/catalog']);
  });

  it('shows Graph RAG mode toggle when enabled and displays executed GQL metadata', async () => {
    const graphResult = makeFullResult();
    graphResult.question = 'Compare Lincoln and Washington on unity';
    graphResult.mode = 'graph';
    graphResult.gql_queries = ["MATCH (s:Speech)-[r:addressesTopic]->(o:Topic) WHERE LOWER(o.name) CONTAINS 'unity' RETURN s.name AS subject, r.predicate AS predicate, o.name AS object LIMIT 20"];
    graphResult.graph = {
      ...graphResult.graph,
      basis: 'foundry_graph_index',
    };
    const fetch = installFetch(async () => json(graphResult), {
      health: json({ ...fullHealth, graph_rag_available: true }),
      catalog: json(makeFullCatalog()),
    });
    render(<App />);
    const graphBtn = await screen.findByRole('button', { name: 'Graph RAG (GQL)' });
    await userEvent.click(graphBtn);
    expect(graphBtn).toHaveAttribute('aria-pressed', 'true');
    await submit('Compare Lincoln and Washington on unity');
    expect(await screen.findByText(/The test passage connects work/)).toBeInTheDocument();
    expect(fetch).toHaveBeenCalledWith('/api/ask', expect.objectContaining({
      body: JSON.stringify({ question: 'Compare Lincoln and Washington on unity', mode: 'graph' }),
    }));
    await userEvent.click(screen.getByText('Response metadata'));
    expect(screen.getByText(/MATCH \(s:Speech\)-\[r:addressesTopic\]/)).toBeInTheDocument();
    await viewTab('Graph');
    expect(screen.getByRole('heading', { name: 'Knowledge graph traversal' })).toBeInTheDocument();

    // Selecting a RAG history entry restores the Standard RAG mode toggle.
    const ragBtn = screen.getByRole('button', { name: 'Standard RAG' });
    await userEvent.click(ragBtn);
    expect(ragBtn).toHaveAttribute('aria-pressed', 'true');
    await userEvent.click(screen.getByRole('button', { name: /Compare Lincoln and Washington on unity/ }));
    expect(screen.getByRole('button', { name: 'Graph RAG (GQL)' })).toHaveAttribute('aria-pressed', 'true');
  });
});
