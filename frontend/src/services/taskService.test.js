import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('./apiService', () => ({
  default: {
    post: vi.fn(),
    get: vi.fn(),
  },
}));

import apiService from './apiService';
import taskService from './taskService';

describe('taskService', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('cancelTask posts to the M3 cancel endpoint', async () => {
    apiService.post.mockResolvedValue({
      task_id: 't1',
      status: 'CANCELLED',
      cancelled_at: null,
    });

    const result = await taskService.cancelTask('t1');

    expect(apiService.post).toHaveBeenCalledTimes(1);
    expect(apiService.post).toHaveBeenCalledWith('/api/tasks/t1/cancel');
    expect(result.status).toBe('CANCELLED');
  });

  it('retryTask posts to the M3 retry endpoint', async () => {
    apiService.post.mockResolvedValue({
      task_id: 't2',
      status: 'PENDING',
      retry_count: 1,
    });

    const result = await taskService.retryTask('t2');

    expect(apiService.post).toHaveBeenCalledTimes(1);
    expect(apiService.post).toHaveBeenCalledWith('/api/tasks/t2/retry');
    expect(result.status).toBe('PENDING');
    expect(result.retry_count).toBe(1);
  });

  it('getTask fetches the existing task detail endpoint for refresh', async () => {
    apiService.get.mockResolvedValue({
      id: 't3',
      status: 'FAILED',
      result: null,
      error: 'boom',
    });

    const result = await taskService.getTask('t3');

    expect(apiService.get).toHaveBeenCalledTimes(1);
    expect(apiService.get).toHaveBeenCalledWith('/api/tasks/t3');
    expect(result.status).toBe('FAILED');
  });

  it('propagates request failures to the caller', async () => {
    apiService.post.mockRejectedValue(new Error('API request failed: 409'));

    await expect(taskService.retryTask('t4')).rejects.toThrow(
      'API request failed: 409',
    );
  });
});
