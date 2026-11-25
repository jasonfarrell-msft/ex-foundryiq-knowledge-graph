import { fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it } from 'vitest';
import { CatalogBrowser, CATALOG_PAGE_SIZE } from './CatalogBrowser';
import { catalog, makeFullCatalog } from './test/fixtures';

describe('bounded, searchable corpus catalog', () => {
  it('makes all 1,060 records reachable while rendering at most one page', async () => {
    const user = userEvent.setup();
    render(<CatalogBrowser catalog={makeFullCatalog()} />);
    const list = screen.getByRole('list', { name: 'Speech catalog results' });
    expect(within(list).getAllByRole('listitem')).toHaveLength(CATALOG_PAGE_SIZE);
    expect(screen.getByRole('status')).toHaveTextContent('Showing 1–25 of 1,060 matching speeches');
    expect(screen.getByText('Page 1 of 43')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Previous page' })).toBeDisabled();
    const seen = new Set<string>();
    for (let page = 1; page <= 43; page++) {
      const items = within(list).getAllByRole('listitem');
      expect(items.length).toBeLessThanOrEqual(CATALOG_PAGE_SIZE);
      for (const item of items) seen.add(item.textContent ?? '');
      if (page < 43) await user.click(screen.getByRole('button', { name: 'Next page' }));
    }
    expect(seen.size).toBe(1060);
    expect(screen.getByRole('status')).toHaveTextContent('Showing 1,051–1,060 of 1,060');
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Previous page' }));
    expect(screen.getByText('Page 42 of 43')).toBeInTheDocument();
  });

  it.each(['gettysburg', 'LINCOLN', '1863-11-19', 'Gettysburg Lincoln 1863'])('searches the whole catalog by %s, not just the rendered page', query => {
    render(<CatalogBrowser catalog={makeFullCatalog()} />);
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: query } });
    expect(screen.getByText('Gettysburg Address test record')).toBeInTheDocument();
    expect(screen.getByRole('status')).toHaveTextContent('Showing 1–1 of 1 matching speeches');
    expect(screen.getByRole('status')).toHaveTextContent('1,060 catalog records total');
  });

  it('resets pagination on search, explains no matches, and restores all records on clear', async () => {
    const user = userEvent.setup();
    render(<CatalogBrowser catalog={makeFullCatalog()} />);
    await user.click(screen.getByRole('button', { name: 'Next page' }));
    fireEvent.change(screen.getByRole('searchbox'), { target: { value: 'absent phrase' } });
    expect(screen.getByRole('status')).toHaveTextContent('No speeches match this filter');
    expect(screen.getByText('Page 1 of 1')).toBeInTheDocument();
    expect(screen.getByText(/No catalog records have been removed/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear filter' }));
    expect(screen.getByRole('status')).toHaveTextContent('Showing 1–25 of 1,060');
    expect(screen.getByText('Page 1 of 43')).toBeInTheDocument();
  });

  it('supports a legacy sample and warns when catalog and coverage counts differ', () => {
    render(<CatalogBrowser catalog={catalog} />);
    expect(screen.getByText('10 sources · limited sample')).toBeInTheDocument();
    expect(screen.getByText(/Catalog count differs from reported coverage/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Next page' })).toBeDisabled();
  });
});
