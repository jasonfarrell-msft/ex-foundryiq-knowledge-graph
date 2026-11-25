import Markdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import type { Root } from 'mdast';
import type { Citation } from './types';

interface AnswerProps {
  answer: string | null;
  citations: Citation[];
  onCitation: (id: string) => void;
}

interface MarkdownNode {
  type: string;
  value?: string;
  url?: string;
  children?: MarkdownNode[];
}

export function Answer({ answer, citations, onCitation }: AnswerProps) {
  if (!answer?.trim()) {
    return <p className="notice">No narrative answer was returned. Review any available passages and relationships, and the service notices below.</p>;
  }
  const byId = new Map(citations.map((citation, index) => [citation.id, { citation, index }]));
  function citationPlugin() {
    return (tree: Root) => {
      function visit(node: MarkdownNode) {
        // Do not rewrite code literals or introduce nested links.
        if (!node.children || node.type === 'link' || node.type === 'linkReference') return;
        node.children = node.children.flatMap(child => {
          if (child.type !== 'text' || child.value === undefined) {
            visit(child);
            return [child];
          }
          return child.value.split(/(\[ref_id:[^\]\r\n]+\])/g).map(part => {
            const match = /^\[ref_id:([^\]\r\n]+)\]$/.exec(part);
            const known = match ? byId.get(match[1]) ?? byId.get(part) ?? byId.get(part.slice(1, -1)) : undefined;
            return known ? {
              type: 'link', url: `#speech-citation-${known.index}`,
              children: [{ type: 'text', value: String(known.index + 1) }],
            } : { type: 'text', value: part };
          });
        });
      }
      visit(tree);
    };
  }

  return <div className="answer-text answer-markdown">
    <Markdown remarkPlugins={[remarkGfm, citationPlugin]} skipHtml disallowedElements={['img']} components={{
      h1: ({ children }) => <h3>{children}</h3>,
      h2: ({ children }) => <h4>{children}</h4>,
      h3: ({ children }) => <h5>{children}</h5>,
      h4: ({ children }) => <h6>{children}</h6>,
      h5: ({ children }) => <h6>{children}</h6>,
      h6: ({ children }) => <h6>{children}</h6>,
      a: ({ href, children }) => {
        const match = /^#speech-citation-(\d+)$/.exec(href ?? '');
        const index = match ? Number(match[1]) : -1;
        const citation = citations[index];
        if (!citation) return <span>{children}</span>;
        return <button className="citation-mark" type="button"
          aria-label={`Read passage ${index + 1}: ${citation.title}`}
          onClick={() => onCitation(citation.id)}>{index + 1}</button>;
      },
    }}>{answer}</Markdown>
  </div>;
}
