import { useEffect, useState } from "react";
import RailNavigator from "./RailNavigator";
import DrawerSystem from "./DrawerSystem";
import NodeInspector from "../visualization/NodeInspector";
import ChatWorkspace from "../chat/ChatWorkspace";
import authService from "../../services/authService";
import { formatBytes, formatDateTime, formatScore } from "../../utils/helpers";
import { API_URL, CHAT_PROVIDER } from "../../utils/constants";

/**
 * MainLayout - Obsidian-style workspace shell.
 *
 * Left: compact workspace rail (Mind / Chat / Tasks / Documents /
 * Memory / Skills / Settings). Center: the active workspace — the
 * Mind knowledge graph or the Chat conversation. Right: contextual
 * NodeInspector while a node is selected, minimal system overview
 * otherwise. Bottom: one compact command bar (ask GHOST / compose).
 */
export default function MainLayout({
  children,
  activeTab,
  onTabChange,
  backendOnline = false,
  selectedNode = null,
  setSelectedNode = () => {},
  nodeCounts = {},
  inspectorMemory = null,
  inspectorNode = null,
  inspectorConnections = [],
  inspectorTaskDetail = null,
  graphNodes = [],
  setActiveDrawer = () => {},
  activeDrawer = null,
  messages = [],
  message = "",
  setMessage = () => {},
  loading = false,
  uploading = false,
  uploadStatus = "",
  documentId = null,
  documentName = "",
  fileInputRef = null,
  handleFileChange = () => {},
  sendMessage = () => {},
  handleKeyDown = () => {},
  newChat = () => {},
  clearDocument = () => {},
  forgetAllMemories = () => {},
  deleteDocument = () => {},
  documents = [],
  documentsLoading = false,
  memoryItems = [],
  memoryLoading = false,
  deleteMemory = () => {},
  onTaskAction = null,
  renderGhostResponse = () => {},
  onLogout = () => {}
}) {
  const [providerOnline, setProviderOnline] = useState(false);
  const [modelName, setModelName] = useState("");
  const [systemStatus, setSystemStatus] = useState("Checking…");

  // Live provider status through the existing health endpoint.
  useEffect(() => {
    const fetchProviderStatus = async () => {
      try {
        const token = authService.getToken();
        const response = await fetch(`${API_URL}/health/provider?provider=${CHAT_PROVIDER}`, {
          credentials: "include",
          headers: token ? { Authorization: `Bearer ${token}` } : {},
        });
        if (response.ok) {
          const data = await response.json();
          setProviderOnline(data.online ?? true);
          setModelName(data.model || "");

          const label =
            (data.provider || "provider").charAt(0).toUpperCase() +
            (data.provider || "provider").slice(1);

          if ((data.online ?? true) && backendOnline) {
            setSystemStatus(
              `${label} — ${data.model} — Connected`
            );
          } else if (data.online === false) {
            // Diagnostic reasons from /health/provider (metadata
            // only): distinguish quota/availability from outages.
            if (data.reason === "rate_limited") {
              setSystemStatus(
                `${label} — Rate limited — Retrying automatically`
              );
            } else if (data.reason === "unavailable") {
              setSystemStatus(
                `${label} — Temporarily unavailable — Retrying automatically`
              );
            } else {
              setSystemStatus(
                `${label} — Connection error — Retrying automatically`
              );
            }
          } else if (!backendOnline) {
            setSystemStatus("Backend offline");
          } else {
            setSystemStatus("Provider unreachable");
          }
        } else if (response.status === 401) {
          // Session expired/absent: not a provider problem.
          setProviderOnline(false);
          setSystemStatus("Sign in to check provider status");
        } else {
          setProviderOnline(false);
          setSystemStatus("Provider unreachable");
        }
      } catch (error) {
        console.error("Failed to fetch provider status:", error);
        setProviderOnline(false);
        setSystemStatus("Provider unreachable");
      }
    };

    fetchProviderStatus();
    const providerInterval = setInterval(fetchProviderStatus, 30000);

    return () => clearInterval(providerInterval);
  }, [backendOnline]);

  // Real graph node groups for the drawers and overview.
  const taskNodes = graphNodes.filter(
    (graphNode) => graphNode.type === "task" && graphNode.id !== "tasks-root"
  );
  const toolNodes = graphNodes.filter(
    (graphNode) => graphNode.type === "tool"
  );
  const documentNodes = graphNodes.filter(
    (graphNode) => graphNode.type === "document"
  );
  const memoryNodes = graphNodes.filter(
    (graphNode) => graphNode.type === "memory" && graphNode.memory_id
  );

  // Navigate the inspector to a connected node.
  const openInspectorNode = (nodeId) => {
    const target = graphNodes.find((graphNode) => graphNode.id === nodeId);
    if (target) {
      setSelectedNode(target);
    }
  };

  // Chat context: the node selected in the Mind, carried into the
  // conversation so the user can ask GHOST about it.
  const contextNode =
    selectedNode && selectedNode.id !== "GHOST"
      ? { type: selectedNode.type, label: selectedNode.label || selectedNode.id }
      : null;

  const isChatWorkspace = activeTab === "chat";

  // In-app update control: checks the desktop shell's HTTPS
  // manifest lookup (/enma-update-check). If a newer trusted
  // release exists, the download URL opens in the user's
  // default browser and the normal signed installer takes over.
  // ENMA never self-replaces; failures are reported honestly.
  const [updateState, setUpdateState] = useState({
    status: "idle",
    info: null,
  });

  const handleUpdateCheck = async () => {
    setUpdateState({ status: "checking", info: null });
    try {
      const response = await fetch("/enma-update-check");
      const info = await response.json();
      if (info.available) {
        setUpdateState({ status: "available", info });
        window.open(info.url, "_blank", "noopener");
      } else if (info.error) {
        setUpdateState({ status: "error", info });
      } else {
        setUpdateState({ status: "up-to-date", info });
      }
    } catch {
      setUpdateState({
        status: "error",
        info: { error: "update check failed" },
      });
    }
  };

  const updateLabel =
    updateState.status === "checking"
      ? "Checking..."
      : updateState.status === "available"
        ? "Update available"
        : updateState.status === "up-to-date"
          ? "Up to date"
          : updateState.status === "error"
            ? "Update check failed"
            : "Update";

  return (
    <main className="ghost-app main-layout-theme">
      {/* Header */}
      <header className="main-layout-header">
        <div className="top-bar">
          <div className="top-bar-left">
            <span className="brand-mark">E</span>
            <span className="brand-name">ENMA</span>
            <span className="workspace-divider">/</span>
            <span className="workspace-name">
              {isChatWorkspace ? "Chat" : "Mind"}
            </span>
          </div>
          <div className="top-bar-right">
            <span className="header-model">{modelName}</span>
            <button
              className="lock-button update-button"
              type="button"
              onClick={handleUpdateCheck}
              disabled={updateState.status === "checking"}
              title={
                updateState.info && updateState.info.error
                  ? `Update check: ${updateState.info.error}`
                  : "Check for an ENMA update"
              }
            >
              {updateLabel}
            </button>
            <button
              className="lock-button"
              type="button"
              onClick={onLogout}
              title="Log out of ENMA"
            >
              Lock / Log out
            </button>
          </div>
        </div>
      </header>

      {/* Main Layout Grid */}
      <div className="main-layout">
        {/* Left: Workspace Rail */}
        <div className="nav-rail">
          <RailNavigator
            activeTab={activeTab}
            onTabChange={onTabChange}
          />
        </div>

        {/* Center: Active Workspace */}
        <div className="main-layout-main">
          {isChatWorkspace ? (
            <ChatWorkspace
              messages={messages}
              loading={loading}
              contextNode={contextNode}
              onClearContext={() => setSelectedNode(null)}
              onNewChat={newChat}
              onTaskAction={onTaskAction}
              renderGhostResponse={renderGhostResponse}
            />
          ) : (
            children
          )}
        </div>

        {/* Right: Contextual Panel — NodeInspector while a node is
            selected, minimal system overview otherwise. */}
        <div className="main-layout-right">
          {selectedNode ? (
            <NodeInspector
              node={inspectorNode || selectedNode}
              memoryItem={inspectorMemory}
              taskDetail={inspectorTaskDetail}
              systemInfo={{
                model: modelName,
                providerOnline,
              }}
              nodeCounts={nodeCounts}
              connections={inspectorConnections}
              onOpenNode={openInspectorNode}
              onClose={() => setSelectedNode(null)}
            />
          ) : (
            <div className="right-panel-content workspace-overview">
              <div className="overview-brand">
                <div className="overview-title">ENMA</div>
                <div className="overview-sub">
                  Second brain · knowledge workspace
                </div>
              </div>

              <div className="overview-section">
                <div className="overview-label">SYSTEM</div>
                <div className="overview-row">
                  <span>Backend</span>
                  <b className={backendOnline ? "is-ok" : "is-bad"}>
                    {backendOnline ? "Connected" : "Offline"}
                  </b>
                </div>
                <div className="overview-row">
                  <span>Status</span>
                  <b>{systemStatus}</b>
                </div>
              </div>

              <div className="overview-section">
                <div className="overview-label">MIND</div>
                <button
                  className="overview-row"
                  onClick={() => onTabChange("memory")}
                >
                  <span>Memories</span>
                  <b>{memoryNodes.length}</b>
                </button>
                <button
                  className="overview-row"
                  onClick={() => onTabChange("documents")}
                >
                  <span>Documents</span>
                  <b>{documentNodes.length}</b>
                </button>
                <button
                  className="overview-row"
                  onClick={() => onTabChange("tasks")}
                >
                  <span>Tasks</span>
                  <b>{taskNodes.length}</b>
                </button>
                <button
                  className="overview-row"
                  onClick={() => setActiveDrawer("tools")}
                >
                  <span>Tools</span>
                  <b>{toolNodes.length}</b>
                </button>
              </div>

              <div className="overview-hint">
                Select a node in the Mind to inspect it.
              </div>
            </div>
          )}
        </div>
      </div>

      {/* Side Drawer System (knowledge drawers overlay the workspace) */}
      {activeDrawer && activeDrawer !== "chat" && (
        <DrawerSystem
          isOpen={true}
          title={
            activeDrawer === "documents"
              ? "DOCUMENTS"
              : activeDrawer === "memory"
                ? "MEMORY"
                : activeDrawer === "projects"
                  ? "PROJECTS"
                  : activeDrawer === "tasks"
                    ? "TASKS"
                    : activeDrawer === "tools"
                      ? "TOOLS"
                      : activeDrawer === "guardian"
                        ? "GUARDIAN"
                        : "PANEL"
          }
          subtitle={activeDrawer === "documents" && documentName ? `Document: ${documentName}` : undefined}
          onClose={() => setActiveDrawer(null)}
        >
          {/* Documents drawer */}
          {activeDrawer === "documents" && (
            <div className="drawer-body">
              <div className="panel-icon document-icon">▤</div>
              <div className="panel-title">DOCUMENT INTELLIGENCE</div>
              <div className="panel-description">
                Upload PDF, DOCX or TXT files and make them available to ENMA's retrieval and whole-document analysis pipeline.
              </div>
              <button
                className="primary-action"
                onClick={() => fileInputRef.current?.click()}
                disabled={uploading}
              >
                {uploading ? "INGESTING..." : "UPLOAD DOCUMENT"}
              </button>
              {documentName ? (
                <div className="active-doc-card">
                  <div className="card-kicker">ACTIVE DOCUMENT</div>
                  <div className="active-doc-name">{documentName}</div>
                  <div className="active-doc-status">
                    <span />
                    {uploadStatus || "DOCUMENT READY · INDEXED"}
                  </div>
                  <button
                    className="secondary-action"
                    onClick={clearDocument}
                  >
                    CLEAR ACTIVE DOCUMENT
                  </button>
                </div>
              ) : (
                <div className="empty-panel">NO ACTIVE DOCUMENT</div>
              )}

              {/* Document list */}
              <div className="drawer-list-toolbar">
                <span>UPLOADED DOCUMENTS</span>
                <span className="drawer-list-count">
                  {documentsLoading ? "LOADING..." : `${documents.length}`}
                </span>
              </div>
              {documents.length === 0 ? (
                <div className="empty-panel">NO DOCUMENTS UPLOADED</div>
              ) : (
                <div className="doc-list">
                  {documents.map((doc) => (
                    <div
                      className={`doc-row ${
                        doc.document_id === documentId ? "is-active" : ""
                      }`}
                      key={doc.document_id}
                    >
                      <div className="doc-row-head">
                        <span
                          className="doc-name"
                          title={doc.filename}
                        >
                          {doc.filename}
                        </span>
                        <button
                          className="row-delete"
                          onClick={() => deleteDocument(doc.document_id)}
                          disabled={documentsLoading}
                        >
                          DELETE
                        </button>
                      </div>
                      <div className="doc-meta">
                        {doc.pages} PAGES · {doc.chunks} CHUNKS · {formatBytes(doc.size)} · {doc.status}
                        {doc.has_summary ? " · SUMMARIZED" : ""}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {/* Document stats */}
              <div className="drawer-stat-grid">
                <div>
                  <span>DOCUMENT NODES</span>
                  <b>{documentNodes.length}</b>
                </div>
                <div>
                  <span>ACTIVE FILE</span>
                  <b>{documentId ? "YES" : "NO"}</b>
                </div>
              </div>
            </div>
          )}

          {/* Memory drawer */}
          {activeDrawer === "memory" && (
            <div className="drawer-body">
              <div className="panel-icon memory-icon">◈</div>
              <div className="panel-title">PERSISTENT MEMORY</div>
              <div className="panel-description">
                ENMA's persistent knowledge layer stores useful project context, decisions, preferences and facts.
              </div>
              <div className="big-stat">
                <strong>{memoryNodes.length}</strong>
                <span>MEMORY NODES</span>
              </div>
              <div className="info-block">
                <div>STATUS</div>
                <strong>PERSISTENT</strong>
              </div>

              {/* Memory list */}
              <div className="drawer-list-toolbar">
                <span>STORED MEMORIES</span>
                <div className="drawer-list-actions">
                  <span className="drawer-list-count">
                    {memoryLoading ? "LOADING..." : `${memoryItems.length}`}
                  </span>
                  <button
                    className="row-delete"
                    onClick={forgetAllMemories}
                    disabled={memoryLoading || memoryItems.length === 0}
                  >
                    FORGET ALL
                  </button>
                </div>
              </div>
              {memoryItems.length === 0 ? (
                <div className="empty-panel">NO MEMORIES STORED</div>
              ) : (
                <div className="memory-list">
                  {memoryItems.map((memory) => (
                    <div className="memory-row" key={memory.id}>
                      <div className="memory-row-head">
                        <span className={`memory-chip chip-${memory.type}`}>
                          {memory.type}
                        </span>
                        <span className="memory-date">
                          {formatDateTime(memory.created_at)}
                        </span>
                      </div>
                      <div className="memory-content">
                        {memory.content}
                      </div>
                      <div className="memory-row-foot">
                        <span>
                          IMP {formatScore(memory.importance)} · CONF {formatScore(memory.confidence)}
                        </span>
                        <button
                          className="row-delete"
                          onClick={() => deleteMemory(memory.id)}
                          disabled={memoryLoading}
                        >
                          DELETE
                        </button>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Tasks drawer */}
          {activeDrawer === "tasks" && (
            <div className="drawer-body">
              <div className="panel-icon task-icon">✓</div>
              <div className="panel-title">TASK EXECUTION</div>
              <div className="panel-description">
                Tasks planned through ENMA appear here and as nodes in the neural graph.
              </div>
              <div className="big-stat">
                <strong>{taskNodes.length}</strong>
                <span>TASK NODES</span>
              </div>
              <div className="drawer-list-toolbar">
                <span>PLANNED TASKS</span>
                <span className="drawer-list-count">{taskNodes.length}</span>
              </div>
              {taskNodes.length === 0 ? (
                <div className="empty-panel">NO TASKS PLANNED</div>
              ) : (
                <div className="doc-list">
                  {taskNodes.map((task) => (
                    <div className="doc-row" key={task.id}>
                      <div className="doc-row-head">
                        <span className="doc-name" title={task.label}>
                          {task.label}
                        </span>
                      </div>
                      <div className="doc-meta">
                        {String(task.status || "—").toUpperCase()} · PRIORITY{" "}
                        {task.priority ?? "—"} · {formatDateTime(task.created_at)}
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}

          {/* Tools drawer */}
          {activeDrawer === "tools" && (
            <div className="drawer-body">
              <div className="panel-icon tool-icon">⚙</div>
              <div className="panel-title">TOOL REGISTRY</div>
              <div className="panel-description">
                The production tools registered in ENMA's permission-gated tool registry.
              </div>
              <div className="big-stat">
                <strong>{toolNodes.length}</strong>
                <span>REGISTERED TOOLS</span>
              </div>
              <div className="drawer-list-toolbar">
                <span>REGISTERED TOOLS</span>
                <span className="drawer-list-count">{toolNodes.length}</span>
              </div>
              {toolNodes.length === 0 ? (
                <div className="empty-panel">NO TOOLS REGISTERED</div>
              ) : (
                <div className="doc-list">
                  {toolNodes.map((tool) => (
                    <div className="doc-row" key={tool.id}>
                      <div className="doc-row-head">
                        <span className="doc-name" title={tool.label}>
                          {tool.label}
                        </span>
                      </div>
                      <div className="doc-meta">
                        {String(tool.category || "—").toUpperCase()} · RISK{" "}
                        {String(tool.risk_level || "—").toUpperCase()}
                      </div>
                      <div className="doc-meta">{tool.description}</div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
        </DrawerSystem>
      )}

      {/* Compact command bar — ask GHOST from the Mind, compose in Chat */}
      <section className="main-layout-chat">
        <div className="command-area-large">
          <span
            className={`command-dot ${loading ? "is-thinking" : uploading ? "is-ingesting" : ""}`}
            title={loading ? "ENMA is thinking" : uploading ? "ENMA is ingesting" : "ENMA is ready"}
          />
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={
              isChatWorkspace
                ? "Message ENMA…"
                : documentName
                  ? `Ask ENMA about ${documentName}…`
                  : "Ask ENMA…"
            }
            disabled={loading}
            rows={1}
            className="command-input-large"
          />
          <button
            className="upload-command"
            onClick={() => fileInputRef.current?.click()}
            disabled={uploading}
            title="Upload document"
          >
            +
          </button>
          <button
            className="send-button"
            onClick={sendMessage}
            disabled={loading || !message.trim()}
            title="Send"
          >
            ↗
          </button>
        </div>
      </section>

      {/* Hidden File Input */}
      <input
        ref={fileInputRef}
        className="hidden-file-input"
        type="file"
        accept=".pdf,.doc,.docx,.txt,application/pdf,text/plain,application/msword,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        onChange={handleFileChange}
      />

      {/* Footer */}
      <div className="main-layout-footer">
        <div className="footer-left">
          ENMA · NEURAL INTERFACE
        </div>
        <div className="footer-right">
          <span
            className={
              backendOnline ? "footer-online" : ""
            }
          >
            ●
          </span>
          {backendOnline
            ? " BACKEND CONNECTED"
            : " BACKEND OFFLINE"}
        </div>
      </div>
    </main>
  );
}
