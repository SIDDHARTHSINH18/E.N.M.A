import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../../services/documentService', () => ({
  default: {
    fetchDocuments: vi.fn(),
    uploadDocument: vi.fn(),
    deleteDocument: vi.fn(),
  },
}));

import documentService from '../../services/documentService';
import DocumentsPanel from './DocumentsPanel';

afterEach(cleanup);

describe('DocumentsPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    window.localStorage.clear();
  });

  it('renders documents returned by fetchDocuments', async () => {
    documentService.fetchDocuments.mockResolvedValue({
      documents: [
        { document_id: 'd1', filename: 'report.pdf', size: 2048, status: 'indexed' },
        { document_id: 'd2', filename: 'notes.txt', size: 128, status: 'indexed' },
      ],
      total: 2,
    });

    render(<DocumentsPanel onClose={() => {}} />);

    expect(await screen.findByText('report.pdf')).toBeTruthy();
    expect(screen.getByText('notes.txt')).toBeTruthy();
    expect(screen.getByText(/2 FILES/)).toBeTruthy();
    expect(documentService.fetchDocuments).toHaveBeenCalledTimes(1);
  });

  it('uploads through uploadDocument and persists the active document locally', async () => {
    documentService.fetchDocuments.mockResolvedValue({ documents: [], total: 0 });
    documentService.uploadDocument.mockResolvedValue({
      document_id: 'd-new',
      filename: 'uploaded.md',
      size: 42,
    });

    render(<DocumentsPanel onClose={() => {}} />);
    await screen.findByText('No documents uploaded yet');

    const input = document.querySelector('.documents-upload-input');
    expect(input).not.toBeNull();
    fireEvent.change(input, {
      target: { files: [new File(['x'], 'uploaded.md', { type: 'text/markdown' })] },
    });

    await waitFor(() => {
      expect(documentService.uploadDocument).toHaveBeenCalledTimes(1);
    });
    await waitFor(() => {
      expect(screen.getByText('uploaded.md')).toBeTruthy();
    });
    expect(window.localStorage.getItem('enma-active-document-id')).toBe('d-new');
  });

  it('deletes a document through deleteDocument and clears local active id', async () => {
    documentService.fetchDocuments.mockResolvedValue({
      documents: [{ document_id: 'd1', filename: 'gone.pdf', size: 10 }],
      total: 1,
    });
    documentService.deleteDocument.mockResolvedValue({ deleted: true });
    window.localStorage.setItem('enma-active-document-id', 'd1');

    render(<DocumentsPanel onClose={() => {}} />);
    await screen.findByText('gone.pdf');

    fireEvent.click(screen.getByLabelText('Delete document'));

    await waitFor(() => {
      expect(documentService.deleteDocument).toHaveBeenCalledWith('d1');
    });
    await waitFor(() => {
      expect(screen.queryByText('gone.pdf')).toBeNull();
    });
    expect(window.localStorage.getItem('enma-active-document-id')).toBeNull();
  });

  it('shows an error state when loading fails', async () => {
    documentService.fetchDocuments.mockRejectedValue(new Error('boom'));

    render(<DocumentsPanel onClose={() => {}} />);

    expect(await screen.findByText('Failed to load documents')).toBeTruthy();
  });
});
