import { describe, expect, it, vi } from 'vitest';
import { ApiError, askQuestion, getCatalog, getHealth } from './api';
import { health, json, makeResult } from './test/fixtures';

describe('same-origin API client', () => {
  it('posts only the question with credentials and preserves opaque IDs', async () => {
    const fetch = vi.fn().mockResolvedValue(json(makeResult()));
    vi.stubGlobal('fetch', fetch);
    const result = await askQuestion('A question');
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenCalledWith('/api/ask', expect.objectContaining({
      method: 'POST',
      credentials: 'same-origin',
      redirect: 'error',
      headers: { Accept: 'application/json', 'Content-Type': 'application/json' },
      body: JSON.stringify({ question: 'A question' }),
      signal: expect.any(AbortSignal),
    }));
    expect(result.graph.edges.map(edge => edge.id)).toEqual(['edge:opaque:1', 'edge:opaque:2']);
    expect(result.citations[0].chunk_id).toBe('opaque/chunk:1');
  });

  it('sends mode=graph when graph retrieval mode is selected', async () => {
    const fetch = vi.fn().mockResolvedValue(json(makeResult({ mode: 'graph', gql_queries: ['MATCH (s:Speech)-[r:addressesTopic]->(o:Topic) RETURN s.name LIMIT 5'] })));
    vi.stubGlobal('fetch', fetch);
    const result = await askQuestion('A graph question', undefined, 'graph');
    expect(fetch).toHaveBeenCalledWith('/api/ask', expect.objectContaining({
      body: JSON.stringify({ question: 'A graph question', mode: 'graph' }),
    }));
    expect(result.mode).toBe('graph');
  });

  it('reads only API endpoints for health and catalog', async () => {
    const fetch = vi.fn().mockImplementation(async () => json(health));
    vi.stubGlobal('fetch', fetch);
    await getHealth();
    await getCatalog();
    expect(fetch.mock.calls.map(call => call[0])).toEqual(['/api/health', '/api/catalog']);
  });

  it.each([401, 403, 429, 503, 504])('surfaces HTTP %s without retrying', async status => {
    const fetch = vi.fn().mockResolvedValue(json({}, status, { 'Retry-After': '60' }));
    vi.stubGlobal('fetch', fetch);
    await expect(askQuestion('A question')).rejects.toMatchObject({
      status,
      retryAfter: '60',
    });
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('rejects HTML rather than rendering a sign-in page as an answer', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('<html>sign in</html>', { headers: { 'Content-Type': 'text/html' } })));
    await expect(askQuestion('A question')).rejects.toBeInstanceOf(ApiError);
  });

  it('preserves bounded backend diagnostics instead of hiding every failure behind HTTP 502', async () => {
    const message = 'Azure Search rejected the request (HTTP 400). No automatic retry was made.';
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: message }, 502)));
    await expect(askQuestion('A question')).rejects.toMatchObject({ message, status: 502 });
  });

  it('falls back to a status message for oversized backend diagnostics', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ error: 'x'.repeat(501) }, 502)));
    await expect(askQuestion('A question')).rejects.toThrow('HTTP 502');
  });
  it('rejects malformed or out-of-scope answer envelopes', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...makeResult(), graph: { nodes: [], edges: [], scope: 'entire_corpus' } })));
    await expect(askQuestion('A question')).rejects.toThrow('incomplete response');
  });

  it('times out at 42 seconds without retrying', async () => {
    vi.useFakeTimers();
    const fetch = vi.fn().mockImplementation((_, init: RequestInit) => new Promise((_, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    vi.stubGlobal('fetch', fetch);
    const rejection = expect(askQuestion('A question')).rejects.toThrow('42 seconds');
    await vi.advanceTimersByTimeAsync(42_000);
    await rejection;
    expect(fetch).toHaveBeenCalledTimes(1);
  });

  it('supports caller cancellation', async () => {
    const fetch = vi.fn().mockImplementation((_, init: RequestInit) => new Promise((_, reject) => {
      init.signal?.addEventListener('abort', () => reject(new DOMException('Aborted', 'AbortError')));
    }));
    vi.stubGlobal('fetch', fetch);
    const controller = new AbortController();
    const rejection = expect(askQuestion('A question', controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
    controller.abort();
    await rejection;
    expect(fetch).toHaveBeenCalledTimes(1);
  });
});
