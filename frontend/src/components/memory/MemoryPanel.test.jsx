import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor, fireEvent, cleanup } from '@testing-library/react';

vi.mock('../../services/memoryService', () => ({
  default: {
    fetchMemories: vi.fn(),
    deleteMemory: vi.fn(),
    deleteAllMemories: vi.fn(),
  },
}));

import memoryService from '../../services/memoryService';
import MemoryPanel from './MemoryPanel';

afterEach(cleanup);

describe('MemoryPanel', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.spyOn(window, 'confirm').mockReturnValue(false);
  });

  it('renders memories returned by fetchMemories', async () => {
    memoryService.fetchMemories.mockResolvedValue({
      memories: [
        { id: 'm1', content: 'ENMA uses a task pipeline', source: 'USER' },
        { id: 'm2', content: 'Groq is the planner provider', source: 'SYSTEM' },
      ],
      total: 2,
    });

    render(<MemoryPanel onClose={() => {}} />);

    expect(await screen.findByText('ENMA uses a task pipeline')).toBeTruthy();
    expect(screen.getByText(/2 MEMORIES/)).toBeTruthy();
    expect(memoryService.fetchMemories).toHaveBeenCalledTimes(1);
  });

  it('deletes a memory through deleteMemory and drops it from the list', async () => {
    memoryService.fetchMemories.mockResolvedValue({
      memories: [{ id: 'm1', content: 'to be deleted' }],
      total: 1,
    });
    memoryService.deleteMemory.mockResolvedValue({ deleted: true });

    render(<MemoryPanel onClose={() => {}} />);
    await screen.findByText('to be deleted');

    const deleteButtons = screen.getAllByLabelText('Delete memory');
    fireEvent.click(deleteButtons[0]);

    await waitFor(() => {
      expect(memoryService.deleteMemory).toHaveBeenCalledWith('m1');
    });
    await waitFor(() => {
      expect(screen.queryByText('to be deleted')).toBeNull();
    });
  });

  it('clears all memories through deleteAllMemories after confirmation', async () => {
    memoryService.fetchMemories.mockResolvedValue({
      memories: [{ id: 'm1', content: 'only memory' }],
      total: 1,
    });
    memoryService.deleteAllMemories.mockResolvedValue({ deleted: true, verified: true });
    window.confirm.mockReturnValue(true);

    render(<MemoryPanel onClose={() => {}} />);
    await screen.findByText('only memory');

    fireEvent.click(screen.getByText('CLEAR ALL MEMORIES'));

    await waitFor(() => {
      expect(memoryService.deleteAllMemories).toHaveBeenCalledTimes(1);
    });
  });

  it('shows an error state when loading fails', async () => {
    memoryService.fetchMemories.mockRejectedValue(new Error('boom'));

    render(<MemoryPanel onClose={() => {}} />);

    expect(await screen.findByText('Failed to load memories')).toBeTruthy();
  });
});
