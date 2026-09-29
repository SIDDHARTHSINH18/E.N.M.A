import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor, cleanup } from '@testing-library/react';
import MainLayout from './MainLayout';

// The Update button calls the desktop shell's /enma-update-check
// endpoint (relative URL — only present when running inside the
// packaged app's static server). jsdom fetch is stubbed here.

const baseProps = {
  activeTab: 'chat',
  backendOnline: true,
  messages: [],
  message: '',
  loading: false,
};

describe('MainLayout update control', () => {
  beforeEach(() => {
    vi.stubGlobal('window', Object.assign(window, { open: vi.fn() }));
    window.open = vi.fn();
  });

  afterEach(() => {
    cleanup();
    vi.unstubAllGlobals();
  });

  it('renders a subtle Update control in the header', () => {
    global.fetch = vi.fn().mockResolvedValue({
      json: async () => ({ available: false, current: '0.1.0', latest: null }),
    });

    render(<MainLayout {...baseProps} />);

    const button = screen.getByRole('button', { name: 'Update' });
    expect(button).toBeTruthy();
  });

  it('reports up-to-date honestly when no newer release exists', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      json: async () => ({ available: false, current: '0.1.0', latest: null }),
    });

    render(<MainLayout {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Up to date' })).toBeTruthy();
    });
    expect(window.open).not.toHaveBeenCalled();
  });

  it('opens the trusted HTTPS release URL when an update is available', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      json: async () => ({
        available: true,
        current: '0.1.0',
        latest: 'v0.2.0',
        url: 'https://github.com/example/enma/releases/tag/v0.2.0',
        error: null,
      }),
    });

    render(<MainLayout {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Update available' })).toBeTruthy();
    });
    expect(window.open).toHaveBeenCalledWith(
      'https://github.com/example/enma/releases/tag/v0.2.0',
      '_blank',
      'noopener'
    );
  });

  it('never opens anything for a failed check', async () => {
    global.fetch = vi.fn().mockResolvedValue({
      json: async () => ({
        available: false,
        error: 'manifest fetch failed: HTTP 503',
      }),
    });

    render(<MainLayout {...baseProps} />);
    fireEvent.click(screen.getByRole('button', { name: 'Update' }));

    await waitFor(() => {
      expect(screen.getByRole('button', { name: 'Update check failed' })).toBeTruthy();
    });
    expect(window.open).not.toHaveBeenCalled();
    // The failure reason is surfaced via the button title.
    expect(
      screen.getByRole('button', { name: 'Update check failed' }).getAttribute('title')
    ).toContain('HTTP 503');
  });
});
