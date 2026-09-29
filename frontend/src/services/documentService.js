import apiService from './apiService';

/**
 * Service for handling document operations
 */
const documentService = {
  /**
   * Fetch all documents
   * @returns {Promise<Array>} - List of documents
   */
  fetchDocuments: async () => {
    const response = await apiService.get('/api/documents');
    return response || [];
  },

  /**
   * Upload a document
   * @param {File} file - File to upload
   * @returns {Promise<Object>} - Upload response with document_id
   */
  uploadDocument: async (file) => {
    const formData = new FormData();
    formData.append('file', file);
    return await apiService.upload(formData);
  },

  /**
   * Delete a document
   * @param {string} documentId - ID of document to delete
   * @returns {Promise<Object>} - Delete response
   */
  deleteDocument: async (documentId) => {
    const response = await apiService.delete(`/api/documents/${documentId}`);
    return response;
  },

};

export default documentService;