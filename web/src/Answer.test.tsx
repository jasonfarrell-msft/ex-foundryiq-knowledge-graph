import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { Answer } from './Answer';
import { makeResult } from './test/fixtures';

describe('safe cited answer', () => {
  it('makes known citations selectable and leaves unknown markers as plain text', async () => {
    const onCitation = vi.fn();
    const result = makeResult();
    render(<Answer answer={result.answer} citations={result.citations} onCitation={onCitation} />);
    await userEvent.click(screen.getByRole('button', { name: /Read passage 1/ }));
    expect(onCitation).toHaveBeenCalledWith('0');
    expect(screen.getByText(/Read this unknown reference \[ref_id:unknown\]/)).toBeInTheDocument();
    expect(screen.getAllByRole('button')).toHaveLength(1);
  });

  it('blocks raw HTML, remote images and external or unsafe navigation', () => {
    const { container } = render(<Answer answer={'Safe **finding**.\n\n<img src=x onerror=alert(1)>\n\n[external](https://example.com) [unsafe](javascript:alert%281%29)\n\n![tracking](https://example.com/pixel)\n\n<script>bad()</script>'} citations={[]} onCitation={vi.fn()} />);
    expect(container.querySelector('img, script, a')).toBeNull();
    expect(screen.getByText('finding').tagName).toBe('STRONG');
    expect(screen.getByText('external')).toBeInTheDocument();
    expect(screen.getByText('unsafe')).toBeInTheDocument();
  });

  it('renders headings, emphasis, lists and tables with citations in formatted content', async () => {
    const result = makeResult();
    const onCitation = vi.fn();
    const { container } = render(<Answer answer={'# Shared themes\n\n**National unity** and *economic growth*.\n\n- First theme [ref_id:0]\n- Second theme\n\n1. Compare the speeches\n\n| Theme | Evidence |\n| --- | --- |\n| Unity | [ref_id:0] |\n\n> A quoted passage.'} citations={result.citations} onCitation={onCitation} />);
    expect(screen.getByRole('heading', { name: 'Shared themes', level: 3 })).toBeInTheDocument();
    expect(screen.getByText('National unity').tagName).toBe('STRONG');
    expect(screen.getByText('economic growth').tagName).toBe('EM');
    expect(container.querySelector('ul li')).toHaveTextContent('First theme');
    expect(container.querySelector('ol li')).toHaveTextContent('Compare the speeches');
    expect(screen.getByRole('table')).toBeInTheDocument();
    expect(container.querySelector('blockquote')).toHaveTextContent('A quoted passage.');
    await userEvent.click(within(screen.getByRole('table')).getByRole('button', { name: /Read passage 1/ }));
    expect(onCitation).toHaveBeenCalledWith('0');
  });

  it('preserves citation-like text in inline and fenced code', () => {
    const { container } = render(<Answer answer={'`[ref_id:0]`\n\n```text\n[ref_id:0]\n```'} citations={makeResult().citations} onCitation={vi.fn()} />);
    expect(container.querySelectorAll('code')).toHaveLength(2);
    expect(container.querySelector('pre code')).toHaveTextContent('[ref_id:0]');
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
  });

  it('resolves a prefixed citation ID without changing the opaque ID', async () => {
    const onCitation = vi.fn();
    const citation = { ...makeResult().citations[0], id: 'ref_id:0' };
    render(<Answer answer="A cited finding [ref_id:0]." citations={[citation]} onCitation={onCitation} />);
    await userEvent.click(screen.getByRole('button', { name: /Read passage 1/ }));
    expect(onCitation).toHaveBeenCalledWith('ref_id:0');
  });

  it('explains a missing narrative without hiding other result sections', () => {
    render(<Answer answer={null} citations={[]} onCitation={vi.fn()} />);
    expect(screen.getByText(/No narrative answer was returned/)).toBeInTheDocument();
  });
});
