/**
 * Constants used throughout the GHOST frontend
 */

/**
 * API configuration
 */
// ENMA backend base URL. Deterministic default; VITE_API_URL may override it for non-standard local setups.
export const API_URL = import.meta.env?.VITE_API_URL || "http://127.0.0.1:8000";

/**
 * LocalStorage keys
 */
export const LAYOUT_KEY = "ghost-spatial-layout-v2";
export const ACTIVE_DOCUMENT_KEY = "ghost-active-document-v2";
export const AUTH_TOKEN_KEY = "ghost-auth-token-v1";

// Single active chat provider: every chat request and the
// provider-status poll report THIS provider, so the UI and the
// chat path can never disagree.
export const CHAT_PROVIDER = "groq";

/**
 * Node types and their colors for graph visualization
 */
/**
 * Node types and their colors for graph visualization.
 * Muted, Obsidian-like palette: nodes are the focus,
 * connections and glow stay restrained.
 */
export const NODE_COLORS = {
  ghost: "#dbe7ec",
  document: "#5da285",
  memory: "#5b8dbb",
  project: "#8b6cc9",
  task: "#bf8050",
  tool: "#a3975f",
  system: "#5fa3b0",
  skill: "#b06a6a",
  knowledge: "#4f97a8",
  integration: "#5d90a8",
  // Reserved target types: preserved in the architecture but
  // not yet produced by any backend store (no fabrication).
  conversation: "#6da295",
  entity: "#8f8fb8",
};

/**
 * Zones for positioning different node types in the graph
 * Format: [x, y] coordinates
 */
export const ZONES = {
  document: [-330, -170],
  memory: [-360, 120],
  project: [40, -245],
  task: [120, 190],
  tool: [390, 15],
  system: [-20, -390],
  skill: [200, -100],
  knowledge: [-200, -100],
  integration: [0, 200],
  conversation: [260, -260],
  entity: [-120, 330],
};

/**
 * UI constants
 */
export const DRAWER_WIDTH = 410;

/**
 * GHOST system states
 */
export const GHOST_STATES = {
  IDLE: 'idle',
  LISTENING: 'listening',
  THINKING: 'thinking',
  PLANNING: 'planning',
  EXECUTING: 'executing',
  VERIFYING: 'verifying',
  COMPLETED: 'completed',
  ERROR: 'error',
  APPROVAL_REQUIRED: 'approval_required',
};

/**
 * Task signals that indicate a request should be treated as a task
 * Matches the logic in the original App.jsx
 */
export const TASK_SIGNALS = [
  // Task-creation phrasing: the user must never get a chat-mode
  // "simulated" answer for these (truthfulness fix).
  "create a task",
  "write a file",
  "write a note",
  "create a note",
  "make a file",
  "save a file",
  "check whether",
  "check if",
  "find the file",
  "find a file",
  "create a file",
  "delete a file",
  "read the file",
  "list the files",
  "list files",
  "organize",
  "rename",
  "move the file",
  "copy the file",
  "send an email",
  "send a message",
  "open the application",
  "run the command",
  "execute",
];