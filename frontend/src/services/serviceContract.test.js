import { describe, it, expect, vi, beforeEach } from 'vitest';

// Contract tests: every frontend service call must target a real
// backend endpoint. The backend serves exactly:
//   GET/DELETE /api/memory, DELETE /api/memory/{id}
//   GET /api/documents, DELETE /api/documents/{id}, POST /api/upload
// Anything else was removed from the service layer.

vi.mock('./apiService', () => ({
  default: {
    get: vi.fn(),
    post: vi.fn(),
    put: vi.fn(),
    delete: vi.fn(),
    upload: vi.fn(),
  },
}));

import apiService from './apiService';
import memoryService from './memoryService';
import documentService from './documentService';
import authService from './authService';

describe('memoryService contract', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fetchMemories hits GET /api/memory', async () => {
    apiService.get.mockResolvedValue({ memories: [], total: 0 });

    await memoryService.fetchMemories();

    expect(apiService.get).toHaveBeenCalledWith('/api/memory');
  });

  it('deleteMemory hits DELETE /api/memory/{id}', async () => {
    apiService.delete.mockResolvedValue({ deleted: true });

    await memoryService.deleteMemory('m1');

    expect(apiService.delete).toHaveBeenCalledWith('/api/memory/m1');
  });

  it('deleteAllMemories hits DELETE /api/memory', async () => {
    apiService.delete.mockResolvedValue({ deleted: true, verified: true });

    await memoryService.deleteAllMemories();

    expect(apiService.delete).toHaveBeenCalledWith('/api/memory');
  });

  it('does not call endpoints the backend does not serve', () => {
    expect(memoryService.createMemory).toBeUndefined();
    expect(memoryService.updateMemory).toBeUndefined();
    expect(memoryService.searchMemories).toBeUndefined();
    expect(memoryService.getMemoryStats).toBeUndefined();
    expect(memoryService.fetchAll).toBeUndefined();
    expect(apiService.put).not.toHaveBeenCalled();
  });
});

describe('documentService contract', () => {
  beforeEach(() => vi.clearAllMocks());

  it('fetchDocuments hits GET /api/documents', async () => {
    apiService.get.mockResolvedValue({ documents: [], total: 0 });

    await documentService.fetchDocuments();

    expect(apiService.get).toHaveBeenCalledWith('/api/documents');
  });

  it('uploadDocument posts the file to POST /api/upload', async () => {
    apiService.upload.mockResolvedValue({ document_id: 'd1' });
    const file = new File(['hello'], 'notes.txt', { type: 'text/plain' });

    const result = await documentService.uploadDocument(file);

    expect(apiService.upload).toHaveBeenCalledTimes(1);
    const formData = apiService.upload.mock.calls[0][0];
    expect(formData).toBeInstanceOf(FormData);
    expect(formData.get('file')).toBe(file);
    expect(result.document_id).toBe('d1');
  });

  it('deleteDocument hits DELETE /api/documents/{id}', async () => {
    apiService.delete.mockResolvedValue({ deleted: true });

    await documentService.deleteDocument('d1');

    expect(apiService.delete).toHaveBeenCalledWith('/api/documents/d1');
  });

  it('does not call endpoints the backend does not serve', () => {
    expect(documentService.setActiveDocument).toBeUndefined();
    expect(documentService.clearActiveDocument).toBeUndefined();
    expect(documentService.searchDocuments).toBeUndefined();
    expect(documentService.fetchDocument).toBeUndefined();
    expect(documentService.fetchAll).toBeUndefined();
  });
});

describe('authService contract', () => {
  it('has no refresh-token call (no such backend route)', () => {
    expect(authService.refreshToken).toBeUndefined();
  });
});
