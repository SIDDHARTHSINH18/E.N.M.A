import { describe, it, expect } from 'vitest';
import { render, screen, cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import { renderInline, renderTextBlock, isSafeHttpUrl } from './markdown';

afterEach(cleanup);

function Wrap({ children }) {
  return <div className="ghost-text-block">{children}</div>;
}

function renderBlock(block) {
  return render(<Wrap>{renderTextBlock(block)}</Wrap>);
}

describe('isSafeHttpUrl', () => {
  it('allows http/https only', () => {
    expect(isSafeHttpUrl('https://www.python.org')).toBe(true);
    expect(isSafeHttpUrl('http://example.com/x')).toBe(true);
    expect(isSafeHttpUrl('javascript:alert(1)')).toBe(false);
    expect(isSafeHttpUrl('file:///C:/secrets.txt')).toBe(false);
    expect(isSafeHttpUrl('data:text/html,hi')).toBe(false);
    expect(isSafeHttpUrl('ftp://x')).toBe(false);
  });
});

describe('markdown rendering (A-J regression cases)', () => {
  it('A. paragraph renders as left-aligned <p>', () => {
    renderBlock('ENMA is a personal AI operating system.');
    const p = document.querySelector('.ghost-text-block p');
    expect(p.textContent).toBe('ENMA is a personal AI operating system.');
  });

  it('B. numbered list keeps the number attached to its content', () => {
    renderBlock('1. First point\n2. Second point\n3. Third point');
    const items = document.querySelectorAll('.ghost-numbered');
    expect(items.length).toBe(3);
    expect(items[0].querySelector('.ghost-numbered-marker').textContent).toBe('1.');
    expect(items[0].querySelector('.ghost-numbered-content').textContent).toBe('First point');
    expect(items[2].querySelector('.ghost-numbered-content').textContent).toBe('Third point');
  });

  it('C. nested bullets are indented under their parent', () => {
    renderBlock('- parent\n  - child');
    const bullets = document.querySelectorAll('.ghost-bullet');
    expect(bullets.length).toBe(2);
    expect(bullets[1].style.marginLeft).toBeTruthy();
  });

  it('D. bullet list keeps the marker with its content', () => {
    renderBlock('- alpha\n- beta');
    const bullets = document.querySelectorAll('.ghost-bullet');
    expect(bullets.length).toBe(2);
    expect(bullets[0].textContent).toContain('alpha');
  });

  it('E. markdown links render as safe anchors', () => {
    const nodes = renderInline('See [Python.org](https://www.python.org) now.');
    render(<Wrap>{nodes}</Wrap>);
    const a = screen.getByText('Python.org');
    expect(a.tagName).toBe('A');
    expect(a.getAttribute('href')).toBe('https://www.python.org');
    expect(a.getAttribute('target')).toBe('_blank');
    expect(a.getAttribute('rel')).toContain('noopener');
  });

  it('F. bare HTTPS URLs render as clickable links', () => {
    const nodes = renderInline('Visit https://www.python.org today.');
    render(<Wrap>{nodes}</Wrap>);
    const a = screen.getByText('https://www.python.org');
    expect(a.tagName).toBe('A');
    expect(a.getAttribute('href')).toBe('https://www.python.org');
  });

  it('G. URLs inside inline code stay code, never links', () => {
    const nodes = renderInline('run `curl https://internal.example/api` now');
    render(<Wrap>{nodes}</Wrap>);
    const code = screen.getByText('curl https://internal.example/api');
    expect(code.tagName).toBe('CODE');
    expect(document.querySelector('a.ghost-link')).toBeNull();
  });

  it('G2. trailing sentence punctuation stays out of bare links', () => {
    renderBlock('Visit https://www.python.org.');
    const a = document.querySelector('a.ghost-link');
    expect(a.getAttribute('href')).toBe('https://www.python.org');
    expect(a.parentElement.textContent).toContain('Visit');
    // The trailing period remains visible prose.
    expect(a.parentElement.textContent.endsWith('.')).toBe(true);
  });

  it('H. tables render as real tables with aligned cells', () => {
    renderBlock(
      '| # | Development | Why it matters |\n' +
      '| --- | --- | --- |\n' +
      '| 1 | Quantum error correction | Fault tolerance |\n' +
      '| 2 | Post-quantum crypto | Migration |'
    );
    const table = document.querySelector('table.ghost-table');
    expect(table).toBeTruthy();
    expect(table.querySelectorAll('thead th').length).toBe(3);
    expect(table.querySelectorAll('tbody tr').length).toBe(2);
    expect(table.querySelector('tbody tr td').textContent).toBe('1');
  });

  it('I. mixed markdown response renders all block types', () => {
    renderBlock(
      '## Summary\n\n' +
      'A paragraph with **bold** text.\n\n' +
      '- bullet one\n' +
      '1. numbered one\n\n' +
      '> a quote\n\n' +
      '| A | B |\n| --- | --- |\n| 1 | 2 |'
    );
    expect(document.querySelector('h3')).toBeTruthy();
    expect(document.querySelector('strong')).toBeTruthy();
    expect(document.querySelectorAll('.ghost-bullet').length).toBe(1);
    expect(document.querySelectorAll('.ghost-numbered').length).toBe(1);
    expect(document.querySelector('blockquote')).toBeTruthy();
    expect(document.querySelector('table.ghost-table')).toBeTruthy();
  });

  it('J. long research response with many sources stays structured', () => {
    const response = [
      '# Quantum Computing — 5 key points',
      '',
      '1. Error correction advances — details at [IBM](https://ibm.com/quantum).',
      '2. Post-quantum migration — see https://nist.gov/crypto.',
      '3. Hardware scaling.',
      '4. Algorithms.',
      '5. Standards.',
      '',
      '| Source | Claim |',
      '| --- | --- |',
      '| [IBM](https://ibm.com/quantum) | Error correction progress |',
      '',
      'SOURCES:',
      '- https://ibm.com/quantum',
      '- https://nist.gov/crypto',
    ].join('\n');
    renderBlock(response);
    expect(document.querySelectorAll('.ghost-numbered').length).toBe(5);
    expect(document.querySelectorAll('a.ghost-link').length).toBeGreaterThanOrEqual(4);
    expect(document.querySelector('table.ghost-table')).toBeTruthy();
  });

  it('K. unsafe schemes never become links', () => {
    const nodes = renderInline('do [click](javascript:alert(1)) and file:///C:/x.txt');
    render(<Wrap>{nodes}</Wrap>);
    expect(document.querySelector('a.ghost-link')).toBeNull();
  });
});
