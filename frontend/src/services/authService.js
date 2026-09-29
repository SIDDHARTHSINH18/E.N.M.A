import apiService from './apiService';
import { AUTH_TOKEN_KEY } from '../utils/constants';

/**
 * Security boundary: the session token lives ONLY in
 * sessionStorage — per-tab, never written to disk-backed
 * localStorage — so a closed ENMA window leaves no reusable
 * credential behind. The legacy localStorage copy is cleared
 * on every access.
 */
function clearLegacyTokenStorage() {
  try {
    // The token used to live in localStorage; remove any stale
    // copy without touching the sessionStorage-owned credential.
    localStorage.removeItem(AUTH_TOKEN_KEY);
  } catch {
    // Best effort.
  }
}

/**
 * Authentication service for handling login, logout, and token management
 */
const authService = {
  /**
   * Check if user is authenticated
   * @returns {boolean} - True if authenticated
   */
  isAuthenticated: () => {
    const token = sessionStorage.getItem(AUTH_TOKEN_KEY);
    clearLegacyTokenStorage();
    return !!token && token !== 'null' && token !== 'undefined';
  },

  /**
   * Get authentication token
   * @returns {string|null} - Auth token or null
   */
  getToken: () => {
    clearLegacyTokenStorage();
    return sessionStorage.getItem(AUTH_TOKEN_KEY);
  },

  /**
   * Store authentication token as the single token source
   * @param {string} token - Auth token to store
   */
  setToken: (token) => {
    sessionStorage.setItem(AUTH_TOKEN_KEY, token);
    clearLegacyTokenStorage();
  },

  /**
   * Login user with credentials
   * @param {string} password - Password for authentication
   * @returns {Promise<Object>} - Login response
   */
  login: async (password) => {
    const response = await apiService.post('/api/auth/login', { password });
    
    if (response.token) {
      sessionStorage.setItem(AUTH_TOKEN_KEY, response.token);
      clearLegacyTokenStorage();
      return response;
    }
    
    throw new Error('Login failed: No token received');
  },

  /**
   * Logout user and clear token
   */
  logout: () => {
    sessionStorage.removeItem(AUTH_TOKEN_KEY);
    clearLegacyTokenStorage();
    // Optionally call logout endpoint
    // apiService.post('/api/logout');
  },

  /**
   * Validate current token
   * @returns {Promise<boolean>} - True if token is valid
   */
  validateToken: async () => {
    if (!authService.isAuthenticated()) {
      return false;
    }
    
    try {
      await apiService.get('/api/auth/session');
      return true;
    } catch (error) {
      authService.logout();
      return false;
    }
  },
};

export default authService;