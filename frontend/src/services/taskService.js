import apiService from './apiService';

/**
 * Service for the M3 task lifecycle endpoints. All task
 * lifecycle traffic goes through here so the UI never
 * duplicates API calls.
 */
const taskService = {
  /**
   * Cancel a PENDING or RUNNING task.
   * @param {string} taskId
   * @returns {Promise<Object>} - { task_id, status, cancelled_at }
   */
  cancelTask: async (taskId) => {
    return apiService.post(`/api/tasks/${encodeURIComponent(taskId)}/cancel`);
  },

  /**
   * Retry a FAILED task (FAILED -> PENDING on the backend).
   * @param {string} taskId
   * @returns {Promise<Object>} - { task_id, status, retry_count }
   */
  retryTask: async (taskId) => {
    return apiService.post(`/api/tasks/${encodeURIComponent(taskId)}/retry`);
  },

  /**
   * Fetch the current stored task state (used to refresh a
   * TaskCard after a lifecycle action).
   * @param {string} taskId
   * @returns {Promise<Object>} - Task state envelope
   */
  getTask: async (taskId) => {
    return apiService.get(`/api/tasks/${encodeURIComponent(taskId)}`);
  },
};

export default taskService;
