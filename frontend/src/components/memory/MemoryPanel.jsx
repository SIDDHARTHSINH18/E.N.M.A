import React, { useEffect, useState } from "react";
import memoryService from "../../services/memoryService";
import { formatDateTime } from "../../utils/helpers";

/**
 * MemoryPanel - Interface for browsing, searching, and managing memories
 * Provides CRUD operations for the GHOST memory system
 */
export default function MemoryPanel({ onClose }) {
  const [memories, setMemories] = useState([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [filteredMemories, setFilteredMemories] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Fetch memories on mount and when search changes
  useEffect(() => {
    const loadMemories = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await memoryService.fetchMemories();
        const list = data.memories || [];
        setMemories(list);
        applySearchFilter(list);
      } catch (err) {
        setError("Failed to load memories");
        console.error("Memory load error:", err);
      } finally {
        setLoading(false);
      }
    };

    loadMemories();
  }, [searchQuery]);

  // Apply search filter
  const applySearchFilter = (source = memories) => {
    if (!searchQuery.trim()) {
      setFilteredMemories(source);
      return;
    }

    const queryLower = searchQuery.toLowerCase();
    const filtered = source.filter(memory =>
      (memory.content || "").toLowerCase().includes(queryLower) ||
      (memory.tags || []).some(tag => tag.toLowerCase().includes(queryLower)) ||
      (memory.title || "").toLowerCase().includes(queryLower)
    );
    setFilteredMemories(filtered);
  };

  const handleSearchChange = (e) => {
    setSearchQuery(e.target.value);
  };

  const handleDeleteMemory = async (id) => {
    try {
      await memoryService.deleteMemory(id);
      // Remove from local state
      setMemories(prev => prev.filter(m => m.id !== id));
      setFilteredMemories(prev => prev.filter(m => m.id !== id));
    } catch (err) {
      setError("Failed to delete memory");
      console.error("Delete memory error:", err);
    }
  };

  const handleClearAll = async () => {
    if (!window.confirm("Are you sure you want to delete all memories? This cannot be undone.")) {
      return;
    }
    
    try {
      await memoryService.deleteAllMemories();
      setMemories([]);
      setFilteredMemories([]);
    } catch (err) {
      setError("Failed to clear memories");
      console.error("Clear all memories error:", err);
    }
  };

  if (error) {
    return (
      <div className="memory-panel-error">
        <div className="memory-panel-error-icon">⚠️</div>
        <p className="memory-panel-error-message">{error}</p>
        <button className="memory-panel-retry-btn" onClick={() => window.location.reload()}>
          RETRY
        </button>
      </div>
    );
  }

  return (
    <div className="memory-panel memory-panel-theme">
      {/* Panel Header */}
      <div className="memory-panel-header">
        <button className="memory-panel-close" onClick={onClose} aria-label="Close panel">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
        <h2 className="memory-panel-title">
          MEMORY CORE
          <span className="memory-panel-stats">{filteredMemories.length} MEMORIES</span>
        </h2>
      </div>
      
      {/* Search Bar */}
      <div className="memory-panel-search">
        <input
          type="text"
          placeholder="Search memories..."
          value={searchQuery}
          onChange={handleSearchChange}
          className="memory-panel-search-input"
        />
        <button 
          className="memory-panel-search-clear" 
          onClick={() => setSearchQuery("")}
          aria-label="Clear search"
        >
          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
      </div>
      
      {/* Memories List */}
      <div className="memory-panel-list">
        {loading && !memories.length ? (
            <div className="memory-panel-loading">
            <div className="memory-panel-loading-dots">
              <span /> <span /> <span />
            </div>
            <span className="memory-panel-loading-text">LOADING MEMORY CORE...</span>
          </div>
        ) : (
          filteredMemories.length === 0 ? (
            <div className="memory-panel-empty">
              <div className="memory-panel-empty-icon">🧠</div>
              <p className="memory-panel-empty-message">
                {searchQuery 
                  ? `No memories found for "${searchQuery}"` 
                  : "No memories stored yet"}
              </p>
              <button 
                className="memory-panel-empty-btn" 
                onClick={() => setSearchQuery("")}
              >
                SHOW ALL
              </button>
            </div>
          ) : (
            <div className="memory-panel-memories">
              {filteredMemories.map((memory) => (
                <div key={memory.id} className="memory-panel-memory-item">
                  <div className="memory-panel-memory-content">
                    <div className="memory-panel-memory-header">
                      <span className="memory-panel-memory-type">
                        {(memory.metadata?.source || memory.source || "USER").toUpperCase()}
                      </span>
                      <span className="memory-panel-memory-date">
                        {formatDateTime(memory.timestamp || memory.createdAt)}
                      </span>
                    </div>
                    <p className="memory-panel-memory-text">
                      {(memory.content || "").length > 200 
                        ? `${(memory.content || "").substring(0, 200)}...` 
                        : (memory.content || "")}
                      </p>
                      {memory.tags && memory.tags.length > 0 && (
                        <div className="memory-panel-memory-tags">
                          {memory.tags.map((tag, index) => (
                            <span key={index} className="memory-panel-memory-tag">
                              #{tag}
                            </span>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="memory-panel-memory-actions">
                      <button 
                        className="memory-panel-memory-btn" 
                        onClick={() => handleDeleteMemory(memory.id)}
                        aria-label="Delete memory"
                      >
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5">
                          <line x1="18" y1="6" x2="6" y2="18" />
                          <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </div>
                  </div>
              ))}
            </div>
          )
        )}
      </div>
      
      {/* Panel Footer */}
      <div className="memory-panel-footer">
        <button 
          className="memory-panel-footer-btn memory-panel-clear-btn" 
          onClick={handleClearAll}
        >
          CLEAR ALL MEMORIES
        </button>
        <span className="memory-panel-footer-timestamp">
          LAST_UPDATED: {formatDateTime(Date.now())}
        </span>
      </div>
    </div>
  );
}