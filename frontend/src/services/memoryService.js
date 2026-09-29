import apiService from './apiService';

/**
 * Service for handling memory operations
 */
const memoryService = {
  /**
   * Fetch all memories
   * @returns {Promise<Array>} - List of memories
   */
  fetchMemories: async () => {
    const response = await apiService.get('/api/memory');
    return response || [];
  },

  /**
   * Delete a memory
   * @param {string} memoryId - ID of memory to delete
   * @returns {Promise<Object>} - Delete response
   */
  deleteMemory: async (memoryId) => {
    const response = await apiService.delete(`/api/memory/${memoryId}`);
    return response;
  },

  /**
   * Delete all memories
   * @returns {Promise<Object>} - Response
   */
  deleteAllMemories: async () => {
    const response = await apiService.delete('/api/memory');
    return response;
  },

};

export default memoryService;