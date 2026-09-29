import React, { useEffect, useState } from "react";
import documentService from "../../services/documentService";
import { formatDateTime } from "../../utils/helpers";

/**
 * DocumentsPanel - Interface for browsing, uploading, and managing documents
 * Provides CRUD operations for the GHOST document system
 */
const ACTIVE_DOC_KEY = "enma-active-document-id";

const getActiveDocumentId = () => {
  try {
    return window.localStorage.getItem(ACTIVE_DOC_KEY);
  } catch {
    return null;
  }
};

const storeActiveDocumentId = (id) => {
  try {
    if (id) {
      window.localStorage.setItem(ACTIVE_DOC_KEY, id);
    } else {
      window.localStorage.removeItem(ACTIVE_DOC_KEY);
    }
  } catch {
    // Storage unavailable (private mode): selection stays in-memory.
  }
};

export default function DocumentsPanel({ onClose }) {
  const [documents, setDocuments] = useState([]);
  const [activeDocumentId, setActiveDocumentId] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [uploading, setUploading] = useState(false);
  const [selectedFile, setSelectedFile] = useState(null);

  // Fetch documents on mount
  useEffect(() => {
    const loadDocuments = async () => {
      setLoading(true);
      setError(null);
      try {
        const data = await documentService.fetchDocuments();
        setDocuments(data.documents || []);
        // Set active document from localStorage if available
        const activeId = getActiveDocumentId();
        if (activeId) {
          setActiveDocumentId(activeId);
        }
      } catch (err) {
        setError("Failed to load documents");
        console.error("Document load error:", err);
      } finally {
        setLoading(false);
      }
    };

    loadDocuments();
  }, []);

  const handleFileSelect = async (e) => {
    const file = e.target.files[0];
    if (!file) return;

    setSelectedFile(file);
    setUploading(true);

    try {
      const formData = new FormData();
      formData.append("file", file);

      const response = await documentService.uploadDocument(file);
      setDocuments(prev => [...prev, response]);
      setActiveDocumentId(response.document_id);
      storeActiveDocumentId(response.document_id);
      
      setSelectedFile(null);
      setUploading(false);
    } catch (err) {
      setError("Failed to upload document");
      console.error("Document upload error:", err);
      setSelectedFile(null);
      setUploading(false);
    }
  };

  const handleSetActive = async (docId) => {
    try {
      storeActiveDocumentId(docId);
      setActiveDocumentId(docId);
    } catch (err) {
      setError("Failed to set active document");
      console.error("Set active document error:", err);
    }
  };

  const handleDeleteDocument = async (docId) => {
    try {
      await documentService.deleteDocument(docId);
      setDocuments(prev => prev.filter(doc => doc.document_id !== docId));
      if (activeDocumentId === docId) {
        setActiveDocumentId(null);
        storeActiveDocumentId(null);
      }
    } catch (err) {
      setError("Failed to delete document");
      console.error("Document delete error:", err);
    }
  };

  if (error) {
    return (
      <div className="documents-panel-error">
        <div className="documents-panel-error-icon">⚠️</div>
        <p className="documents-panel-error-message">{error}</p>
        <button className="documents-panel-retry-btn" onClick={() => window.location.reload()}>
          RETRY
        </button>
      </div>
    );
  }

  return (
    <div className="documents-panel documents-panel-theme">
      {/* Panel Header */}
      <div className="documents-panel-header">
        <button className="documents-panel-close" onClick={onClose} aria-label="Close panel">
          <svg viewBox="0 0 24 24" width="20" height="20" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="18" y1="6" x2="6" y2="18" />
            <line x1="6" y1="6" x2="18" y2="18" />
          </svg>
        </button>
        <h2 className="documents-panel-title">
          DOCUMENT VAULT
          <span className="documents-panel-stats">{documents.length} FILES</span>
        </h2>
      </div>
      
      {/* Upload Section */}
      <div className="documents-panel-upload">
        <div className="documents-upload-area">
          <div className="documents-upload-icon">📎</div>
          <p className="documents-upload-text">
            Drag & drop files here<br/>
            or click to select
          </p>
          <input
            type="file"
            accept=".pdf,.doc,.docx,.txt,.md"
            onChange={handleFileSelect}
            className="documents-upload-input"
          />
        </div>
        
        {selectedFile && (
          <div className="documents-upload-preview">
            <span className="documents-upload-filename">
              {selectedFile.name}
            </span>
            {uploading ? (
              <span className="documents-upload-status">• Processing...</span>
            ) : (
              <span className="documents-upload-status">• Ready to upload</span>
            )}
          </div>
        )}
      </div>
      
      {/* Documents List */}
      <div className="documents-panel-list">
        {loading && !documents.length ? (
            <div className="documents-panel-loading">
            <div className="documents-panel-loading-dots">
              <span /> <span /> <span />
            </div>
            <span className="documents-panel-loading-text">LOADING DOCUMENT VAULT...</span>
          </div>
        ) : (
          documents.length === 0 ? (
            <div className="documents-panel-empty">
              <div className="documents-panel-empty-icon">📁</div>
              <p className="documents-panel-empty-message">
                No documents uploaded yet
              </p>
            </div>
          ) : (
            <div className="documents-panel-documents">
              {documents.map((doc) => {
                const isActive = doc.document_id === activeDocumentId;
                return (
                  <div key={doc.document_id} className={`documents-panel-document-item ${isActive ? "active" : ""}`}>
                    <div className="documents-panel-document-content">
                      <div className="documents-panel-document-header">
                        <span className="documents-panel-doc-type">
                          {doc.type ? 
                            `.${doc.type.split('/').pop().toUpperCase()}` : 
                            "DOC"
                          }
                        </span>
                        <span className="documents-panel-doc-filename">
                          {doc.name || doc.filename || "Untitled"}
                        </span>
                        <span className="documents-panel-doc-size">
                          {doc.size ? 
                            (doc.size > 1024 * 1024 
                              ? `${(doc.size / (1024 * 1024)).toFixed(2)} MB` 
                              : `${doc.size} B`) : 
                            "—"
                          }
                        </span>
                      </div>
                      <div className="documents-panel-document-meta">
                        <span className="documents-panel-doc-date">
                          {doc.uploadedAt ? 
                            doc.uploadedAt.substring(0, 10) : 
                            "—"
                          }
                        </span>
                        {isActive && (
                          <span className="documents-panel-doc-active">
                            ACTIVE
                          </span>
                        )}
                      </div>
                    </div>
                    <div className="documents-panel-document-actions">
                      {!isActive && (
                        <button 
                          className="documents-panel-doc-btn" 
                          onClick={() => handleSetActive(doc.document_id)}
                          aria-label="Set as active document"
                        >
                          <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" strokeWidth="1.5">
                            <circle cx="12" cy="12" r="10" strokeDasharray="4 2" />
                            <path d="M12 8v4l2 2" />
                          </svg>
                        </button>
                      )}
                      <button 
                        className="documents-panel-doc-btn documents-panel-delete-btn"
                        onClick={() => handleDeleteDocument(doc.document_id)}
                        aria-label="Delete document"
                      >
                        <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="1.5">
                          <line x1="18" y1="6" x2="6" y2="18" />
                          <line x1="6" y1="6" x2="18" y2="18" />
                        </svg>
                      </button>
                    </div>
                  </div>
                );
              })}
            </div>
          )
        )}
      </div>
      
      {/* Panel Footer */}
      <div className="documents-panel-footer">
        <span className="documents-panel-footer-timestamp">
          LAST_UPDATED: {formatDateTime(Date.now())}
        </span>
      </div>
    </div>
  );
}