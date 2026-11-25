import type { AskResult, Catalog, Health } from './types';

export class ApiError extends Error {
  constructor(
    message: string,
    public readonly status = 0,
    public readonly retryAfter: string | null = null,
  ) {
    super(message);
    this.name = 'ApiError';
  }
}

function statusMessage(status: number): string {
  if (status === 401 || status === 403) {
    return 'The application gateway could not authorize this request. No user login is required; ask your administrator to check the linked API configuration.';
  }
  if (status === 429) return 'The service is busy or a usage limit was reached. Please retry later.';
  if (status === 503) return 'The speech service is unavailable or not configured. Please ask your administrator to check its configuration.';
  if (status === 504) return 'Retrieval exceeded its deadline. Azure work and charges may continue. No automatic retry was made.';
  return `The speech service could not complete this request (HTTP ${status}).`;
}

async function request<T>(
  path: '/health' | '/catalog' | '/ask',
  options: { signal?: AbortSignal; question?: string } = {},
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const cancel = () => controller.abort();
  options.signal?.addEventListener('abort', cancel, { once: true });
  if (options.signal?.aborted) controller.abort();
  // The backend has a 38-second budget; leave time for its response.
  const timeout = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, 42_000);
  try {
    const response = await fetch(`/api${path}`, {
      method: path === '/ask' ? 'POST' : 'GET',
      headers: {
        Accept: 'application/json',
        ...(path === '/ask' ? { 'Content-Type': 'application/json' } : {}),
      },
      ...(path === '/ask' ? { body: JSON.stringify({ question: options.question }) } : {}),
      credentials: 'same-origin',
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response.ok) {
      let message = statusMessage(response.status);
      if (response.headers.get('content-type')?.includes('application/json')) {
        try {
          const body = await response.json();
          if (typeof body?.error === 'string' && body.error.length <= 500) message = body.error;
        } catch {
          // Preserve the safe status message if the gateway returned invalid JSON.
        }
      }
      throw new ApiError(message, response.status, response.headers.get('Retry-After'));
    }
    // Auth redirects / HTML are not treated as successful API results.
    if (!response.headers.get('content-type')?.includes('application/json')) {
      throw new ApiError('The service returned an unexpected response. Check the linked API configuration.');
    }
    return await response.json() as T;
  } catch (error) {
    if (timedOut) throw new ApiError('The request timed out after 42 seconds. Azure work and charges may continue. No automatic retry was made.');
    if (controller.signal.aborted) throw new DOMException('Request cancelled', 'AbortError');
    if (error instanceof ApiError) throw error;
    throw new ApiError('Could not reach the speech service. Check your connection and API configuration.');
  } finally {
    clearTimeout(timeout);
    options.signal?.removeEventListener('abort', cancel);
  }
}

export const getHealth = (signal?: AbortSignal) => request<Health>('/health', { signal });
export const getCatalog = (signal?: AbortSignal) => request<Catalog>('/catalog', { signal });
export async function askQuestion(question: string, signal?: AbortSignal): Promise<AskResult> {
  const result = await request<AskResult>('/ask', { question, signal });
  if (
    typeof result.request_id !== 'string' || typeof result.question !== 'string' ||
    (result.answer !== null && typeof result.answer !== 'string') ||
    !Array.isArray(result.citations) || !Array.isArray(result.graph?.nodes) ||
    !Array.isArray(result.graph?.edges) || result.graph.scope !== 'cited_passages' ||
    !Array.isArray(result.warnings) || !result.coverage
  ) {
    throw new ApiError('The service returned an incomplete response. The previous result has been kept.');
  }
  return result;
}
