export type RetrievalMode = 'rag' | 'graph';

export interface Coverage {
  label: string;
  source_count: number;
  canonical_count?: number;
  full_corpus_available: boolean;
}

export interface Health {
  status: string;
  configured: boolean;
  graph_rag_available?: boolean;
  azure_connectivity?: 'not_probed';
  coverage: Coverage;
  limits: { question_characters: number; graph_edges: number };
  warnings: string[];
}

export interface Speech {
  id: string;
  title: string;
  speaker: string | null;
  date: string | null;
}

export interface Catalog {
  speeches: Speech[];
  coverage: Coverage;
  warnings: string[];
}

export interface Citation extends Speech {
  chunk_id: string;
  text: string;
  source_id: string | null;
  canonical_id?: string | null;
}

export interface GraphNode {
  id: string;
  label: string;
  type: string;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  label: string;
  evidence_ids: string[];
  confidence?: number | null;
}

export interface AskResult {
  request_id: string;
  question: string;
  answer: string | null;
  citations: Citation[];
  graph: {
    nodes: GraphNode[];
    edges: GraphEdge[];
    truncated: boolean;
    scope: 'cited_passages';
    basis?: string;
  };
  warnings: string[];
  coverage: Coverage;
  timing_ms: number;
  partial: boolean;
  mode?: RetrievalMode;
  gql_queries?: string[];
}

export type Selection =
  | { kind: 'citation' | 'node' | 'edge'; id: string }
  | null;
