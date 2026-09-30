import { useEffect, useMemo, useRef, useState } from "react";
// GHOST design system + layout grid (theme first, then layout,
// then App.css so the verified App.css styles keep precedence
// on the few overlapping selectors).
import "./styles/theme.css";
import "./styles/layout.css";
import "./App.css";

import MainLayout from "./components/layout/MainLayout";
import NeuralGraph from "./components/visualization/NeuralGraph";
import authService from "./services/authService";
import graphService from "./services/graphService";
import taskService from "./services/taskService";
import { API_URL } from "./utils/constants";
import { readJSON, saveJSON } from "./utils/helpers";
import { buildVisualGraph } from "./utils/helpers";
import { resolveMemoryNode } from "./utils/helpers";
import { TASK_SIGNALS, CHAT_PROVIDER } from "./utils/constants";
import { renderInline, renderTextBlock } from "./utils/markdown";
import { ZONES } from "./utils/constants";
import CodeBlock from "./components/chat/CodeBlock";
import TaskCard from "./components/chat/TaskCard";

function App() {
  // State variables from original App.jsx
  const [authStatus, setAuthStatus] = useState("checking");

  const [loginPassword, setLoginPassword] = useState("");
  const [loginError, setLoginError] = useState("");
  const [loginLoading, setLoginLoading] = useState(false);

  // First-run passphrase setup (packaged installs): the desktop
  // layer marks config.env as setup-pending; the user then
  // creates their own permanent ENMA passphrase.
  const [setupMode, setSetupMode] = useState(false);
  const [setupPassphrase, setSetupPassphrase] = useState("");
  const [setupConfirm, setSetupConfirm] = useState("");
  const [setupError, setSetupError] = useState("");
  const [setupLoading, setSetupLoading] = useState(false);

  const graphRef = useRef(null);
  const fileInputRef = useRef(null);
  const abortRef = useRef(null);

  const [graphData, setGraphData] = useState({ nodes: [], links: [] });
  const [selectedNode, setSelectedNode] = useState(null);
  const [message, setMessage] = useState("");
  // Conversation persistence: messages (including structured
  // taskData on task responses) survive a page refresh. The
  // structured object is JSON-safe, so TaskCard survives too.
  const [messages, setMessages] = useState(() =>
    readJSON("ghost-chat-history-v1", [])
  );
  const [loading, setLoading] = useState(false);

  // Persist the conversation (with taskData) whenever it
  // changes, bounded to the same recent window the UI uses.
  useEffect(() => {
    saveJSON("ghost-chat-history-v1", messages.slice(-50));
  }, [messages]);

  // First-run detection: the desktop layer reports whether this
  // installation still needs its permanent passphrase created.
  useEffect(() => {
    if (authStatus === "authenticated") return;

    let cancelled = false;

    const checkSetupPending = async () => {
      try {
        const response = await fetch("/enma-backend-status");

        if (response.status === 404) {
          return; // not the desktop static server
        }

        const data = await response.json().catch(() => ({}));

        if (!cancelled && data.setupPending) {
          setSetupMode(true);
        }
      } catch {
        // Static server unreachable — nothing to detect.
      }
    };

    checkSetupPending();

    return () => {
      cancelled = true;
    };
  }, [authStatus]);

  const [uploading, setUploading] = useState(false);
  const [uploadStatus, setUploadStatus] = useState("");
  const [backendOnline, setBackendOnline] = useState(false);

  const storedDocument = readJSON("ghost-active-document-v2", null);
  const [documentId, setDocumentId] = useState(storedDocument?.id || null);
  const [documentName, setDocumentName] = useState(storedDocument?.name || "");

  const [activeDrawer, setActiveDrawer] = useState(null);
  const [activeTab, setActiveTab] = useState("mind");
  const [inspectorTaskDetail, setInspectorTaskDetail] = useState(null);

  // Memory and document management state
  const [memoryItems, setMemoryItems] = useState([]);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [documentItems, setDocumentItems] = useState([]);
  const [documentsLoading, setDocumentsLoading] = useState(false);

  // Authentication functions
  function clearAuthSession() {
    authService.logout();

    setAuthStatus("unauthenticated");
    setLoginPassword("");
  }

  function handleSessionExpired() {
    clearAuthSession();
    setLoginError("Your ENMA session expired. Please log in again.");
  }

  async function authFetch(url, options = {}) {
    const token = authService.getToken();

    if (!token) {
      throw new Error("ENMA authentication is required.");
    }

    const headers = new Headers(options.headers || {});
    headers.set("Authorization", `Bearer ${token}`);

    const response = await fetch(url, { ...options, headers });

    if (response.status === 401) {
      handleSessionExpired();
    }

    return response;
  }

  async function verifyStoredSession(token) {
    try {
      const response = await fetch(`${API_URL}/api/auth/session`, {
        headers: { Authorization: `Bearer ${token}` },
      });

      if (!response.ok) {
        throw new Error("Stored session is no longer valid.");
      }

      setAuthStatus("authenticated");
      setLoginError("");
    } catch {
      authService.logout();
      setAuthStatus("unauthenticated");
    }
  }

  async function login() {
    const password = loginPassword.trim();

    if (!password || loginLoading) {
      return;
    }

    setLoginLoading(true);
    setLoginError("");

    try {
      const response = await fetch(`${API_URL}/api/auth/login`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok || !data.token) {
        throw new Error(data.detail || "Authentication failed.");
      }

      try {
        authService.setToken(data.token);
      } catch {
        throw new Error("ENMA could not create a browser session.");
      }

      setAuthStatus("authenticated");
      setLoginPassword("");
      setLoginError("");
    } catch (error) {
      // Network-level failures ("Failed to fetch") usually mean
      // the desktop backend is not running. The desktop layer
      // exposes its diagnostic state on the same origin.
      if (error.message === "Failed to fetch") {
        let backendReason = "";
        let backendSetupPending = false;

        try {
          const statusResponse = await fetch(
            "/enma-backend-status"
          );
          const status = await statusResponse.json();
          backendReason = status.reason || "";
          backendSetupPending = Boolean(status.setupPending);
        } catch {
          // Status endpoint unavailable (e.g. Vite dev server).
        }

        if (backendSetupPending) {
          // First run: offer the create-passphrase experience.
          setSetupMode(true);
        }

        setLoginError(
          "ENMA backend is not reachable at " +
          `${API_URL}.` +
          (backendReason ? ` ${backendReason}` : "")
        );
      } else {
        setLoginError(error.message || "Authentication failed.");
      }
    } finally {
      setLoginLoading(false);
    }
  }

  async function completeSetup() {
    if (setupLoading) {
      return;
    }

    const passphrase = setupPassphrase.trim();
    const confirm = setupConfirm.trim();

    setSetupError("");

    if (passphrase.length < 8) {
      setSetupError(
        "Passphrase must be at least 8 characters."
      );
      return;
    }

    if (passphrase !== confirm) {
      setSetupError("Passphrases do not match.");
      return;
    }

    setSetupLoading(true);

    try {
      const response = await fetch(`${API_URL}/api/auth/setup`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          passphrase,
          confirm,
        }),
      });

      const data = await response.json().catch(() => ({}));

      if (!response.ok || !data.token) {
        throw new Error(
          data.detail || "Passphrase setup failed."
        );
      }

      authService.setToken(data.token);
      setAuthStatus("authenticated");
      setSetupPassphrase("");
      setSetupConfirm("");
    } catch (error) {
      setSetupError(error.message || "Passphrase setup failed.");
    } finally {
      setSetupLoading(false);
    }
  }

  async function logout() {
    const token = authService.getToken();

    try {
      if (token) {
        await fetch(`${API_URL}/api/auth/session`, {
          method: "DELETE",
          headers: { Authorization: `Bearer ${token}` },
        });
      }
    } catch (error) {
      console.error("GHOST logout error:", error);
    } finally {
      clearAuthSession();
    }
  }

  // Effect hooks
  useEffect(() => {
    let cancelled = false;

    async function restoreSession() {
      let token;

      try {
        token = authService.getToken() || "";
      } catch {
        token = "";
      }

      if (!token) {
        if (!cancelled) {
          setAuthStatus("unauthenticated");
        }
        return;
      }

      if (!cancelled) {
        await verifyStoredSession(token);
      }
    }

    restoreSession();

    return () => {
      cancelled = true;
    };
  }, []);

  // apiService clears the stored token and dispatches "authExpired"
  // whenever an authenticated request receives a 401 (for example
  // from MainLayout's status polling). Route that into the same
  // session-expired flow used by authFetch so the UI returns to
  // login. The handler is defined inline (mirroring
  // handleSessionExpired) so this effect only closes over stable
  // references.
  useEffect(() => {
    function handleAuthExpired() {
      authService.logout();
      setAuthStatus("unauthenticated");
      setLoginPassword("");
      setLoginError("Your ENMA session expired. Please log in again.");
    }

    window.addEventListener("authExpired", handleAuthExpired);
    return () => window.removeEventListener("authExpired", handleAuthExpired);
  }, []);

  useEffect(() => {
    if (authStatus !== "authenticated") {
      return undefined;
    }

    loadGraph();

    const interval = setInterval(
      loadGraph,
      loading ? 12000 : 7000
    );

    return () => clearInterval(interval);
  }, [loading, authStatus]);

  useEffect(() => {
    if (documentId && documentName) {
      saveJSON("ghost-active-document-v2", { id: documentId, name: documentName });
    }
  }, [documentId, documentName]);

  // Graph functions
  async function loadGraph() {
    try {
      // Authenticated graph request through the shared API layer
      // (Phase 2): apiService attaches the Bearer token and, on a
      // 401, clears it and dispatches "authExpired" — the existing
      // listener below then routes into the session-expired flow.
      const data = await graphService.fetchGraphData();

      if (!data || !data.success) {
        throw new Error("Invalid graph response");
      }

      setBackendOnline(true);
      setGraphData({ nodes: data.nodes || [], links: data.edges || [] });

      // If the selected node disappeared from the graph (deleted
      // memory/document/task), close the inspector instead of
      // showing stale data.
      const nextNodes = data.nodes || [];
      setSelectedNode((current) =>
        current && nextNodes.length && !nextNodes.some((node) => node.id === current.id)
          ? null
          : current
      );
    } catch (error) {
      console.error("GHOST graph error:", error);
      setBackendOnline(false);
    }
  }

  // Memory management
  async function loadMemories() {
    setMemoryLoading(true);
    try {
      const response = await authFetch(`${API_URL}/api/memory`);
      if (!response.ok) {
        throw new Error("Memory request failed");
      }
      const data = await response.json();
      setMemoryItems(data.memories || []);
    } catch (error) {
      console.error("GHOST memory error:", error);
    } finally {
      setMemoryLoading(false);
    }
  }

  async function deleteMemory(memoryId) {
    if (!window.confirm("Delete this memory permanently?")) {
      return;
    }

    try {
      const response = await authFetch(`${API_URL}/api/memory/${memoryId}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        throw new Error("Memory deletion failed");
      }
    } catch (error) {
      console.error("GHOST memory delete error:", error);
    }

    await loadMemories();
    loadGraph();
  }

  async function forgetAllMemories() {
    if (!window.confirm("Forget ALL memories? This cannot be undone.")) {
      return;
    }

    try {
      const response = await authFetch(`${API_URL}/api/memory`, {
        method: "DELETE",
      });

      if (!response.ok) {
        throw new Error("Memory cleanup failed");
      }
    } catch (error) {
      console.error("GHOST memory cleanup error:", error);
    }

    await loadMemories();
    loadGraph();
  }

  // Document management
  async function loadDocuments() {
    setDocumentsLoading(true);
    try {
      const response = await authFetch(`${API_URL}/api/documents`);
      if (!response.ok) {
        throw new Error("Document list request failed");
      }
      const data = await response.json();
      setDocumentItems(data.documents || []);
    } catch (error) {
      console.error("GHOST documents error:", error);
    } finally {
      setDocumentsLoading(false);
    }
  }

  async function deleteDocument(documentIdToRemove) {
    if (!window.confirm("Delete this document and its indexed data?")) {
      return;
    }

    try {
      const response = await authFetch(`${API_URL}/api/documents/${documentIdToRemove}`, {
        method: "DELETE",
      });

      if (!response.ok) {
        throw new Error("Document deletion failed");
      }

      if (documentIdToRemove === documentId) {
        clearDocument();
      }
    } catch (error) {
      console.error("GHOST document delete error:", error);
    }

    await loadDocuments();
    loadGraph();
  }

  async function uploadDocument(file) {
    if (!file || uploading) {
      return;
    }

    setUploading(true);
    setUploadStatus(`INGESTING · ${file.name}`);

    try {
      const formData = new FormData();
      formData.append("file", file);

      const response = await authFetch(`${API_URL}/api/upload`, {
        method: "POST",
        body: formData,
      });

      const text = await response.text();
      let data = {};

      try {
        data = JSON.parse(text);
      } catch {
        data = { detail: text };
      }

      if (!response.ok) {
        throw new Error(data.detail || data.message || "Upload failed");
      }

      const id = data.document_id || data.id || data.document?.id;
      if (!id) {
        throw new Error("Upload succeeded but no document ID was returned.");
      }

      const name = data.filename || data.document?.filename || file.name;

      setDocumentId(id);
      setDocumentName(name);
      saveJSON("ghost-active-document-v2", { id, name });

      setUploadStatus("DOCUMENT READY · INDEXED");
      setActiveDrawer("documents");

      await loadGraph();
      await loadDocuments();
    } catch (error) {
      console.error("GHOST upload error:", error);
      setUploadStatus(`UPLOAD ERROR · ${error.message}`);
    } finally {
      setUploading(false);
    }
  }

  function handleFileChange(event) {
    const file = event.target.files?.[0];
    if (file) {
      uploadDocument(file);
    }
    event.target.value = "";
  }

  // Messaging
  function stopGeneration() {
    // Cancels the in-flight request: the streaming reader aborts and
    // the backend stream ends when the client disconnects. Any task
    // already created remains cancellable via its TaskCard.
    abortRef.current?.abort();
  }

  async function sendMessage() {
    if (!message.trim() || loading) return;

    const userMessage = message.trim();
    const controller = new AbortController();
    abortRef.current = controller;
    const signal = controller.signal;

    setMessages((prev) => [
      ...prev,
      { role: "user", content: userMessage },
      { role: "assistant", content: "", pages: [] },
    ]);

    setMessage("");
    setLoading(true);
    setActiveTab("chat");

    const history = messages
      .filter(
        (item) =>
          (item.role === "user" || item.role === "assistant") &&
          item.content &&
          item.content.trim()
      )
      .slice(-12)
      .map((item) => ({
        role: item.role,
        content: item.content,
      }));

    try {
      // TASK ROUTING
      const taskSignals = TASK_SIGNALS;
      const lowerMessage = userMessage.toLowerCase();
      const looksLikeTask = taskSignals.some((signal) => lowerMessage.includes(signal));

      if (looksLikeTask) {
        const response = await authFetch(`${API_URL}/api/tasks`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ request: userMessage }),
          signal,
        });

        const data = await response.json().catch(() => ({}));

        if (!response.ok) {
          throw new Error(
            data.detail || data.message || `Task request failed (${response.status})`
          );
        }

        const execution = data.execution;
        const taskStatus = String(
          execution?.state ||
            execution?.status ||
            data.task?.status ||
            data.status ||
            ""
        ).toUpperCase();

        // Human-facing intro only — the structured object is
        // rendered by TaskCard, never dumped as raw JSON.
        let taskIntro = "Task received.";

        if (taskStatus === "COMPLETED") {
          taskIntro = "Task completed.";
        } else if (
          taskStatus === "PENDING_APPROVAL" ||
          taskStatus === "WAITING_FOR_APPROVAL" ||
          taskStatus === "PAUSED" ||
          taskStatus === "PENDING"
        ) {
          taskIntro =
            "The task is planned but not finished — check the task card below for its actual state (awaiting approval or execution).";
        } else if (taskStatus === "FAILED") {
          taskIntro = "The task could not be completed.";
        }

        // Temporary trace instrumentation (STEP 2): proves the
        // task branch executes and taskData is stored on the
        // message instead of being stringified into it. Remove
        // during frontend cleanup.
        console.info(
          "[ENMA TASK] task branch reached; attaching taskData",
          {
            status: taskStatus,
            hasTaskObject: Boolean(data.task),
            keys: Object.keys(data),
          }
        );

        updateAssistantMessage(taskIntro, [], data);

        setTimeout(loadGraph, 500);
        return;
      }

      // NORMAL CHAT
      // Carry the Mind selection into the conversation so the
      // user can ask GHOST about the node they were inspecting.
      const contextLine =
        selectedNode && selectedNode.id !== "ENMA"
          ? `\n\n(Mind context: the user has the ${selectedNode.type} node "${selectedNode.label}" selected in their knowledge graph.)`
          : "";

      const response = await authFetch(`${API_URL}/api/chat`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          message: userMessage + contextLine,
          provider: CHAT_PROVIDER,
          model: null,
          document_id: documentId,
          history,
        }),
        signal,
      });

      if (!response.ok) {
        const errorText = await response.text();
        throw new Error(errorText || "ENMA request failed");
      }

      if (!response.body) {
        throw new Error("ENMA returned no response body.");
      }

      const reader = response.body.getReader();
      const decoder = new TextDecoder("utf-8");
      let assistantResponse = "";
      let sourcePages = [];
      let lastUpdate = 0;

      while (true) {
        const { value, done } = await reader.read();
        if (done) break;

        const chunk = decoder.decode(value, { stream: true });
        if (!chunk) continue;

        const sourceMatch = chunk.match(/__SOURCES__:([0-9,\s]+)/);
        if (sourceMatch) {
          sourcePages = sourceMatch[1]
            .split(",")
            .map((page) => page.trim())
            .filter(Boolean)
            .map(Number);

          assistantResponse += chunk.replace(/__SOURCES__:[0-9,\s]+\n?/, "");
        } else {
          assistantResponse += chunk;
        }

        const now = performance.now();
        if (now - lastUpdate > 90) {
          updateAssistantMessage(assistantResponse, sourcePages);
          lastUpdate = now;
        }
      }

      assistantResponse += decoder.decode();
      updateAssistantMessage(assistantResponse.trim(), sourcePages);
      setTimeout(() => loadGraph(), 500);
    } catch (error) {
      if (error.name === "AbortError") {
        updateAssistantMessage(
          "CANCELLED — the operation was stopped by the user.",
          []
        );
      } else {
        console.error("GHOST request error:", error);
        updateAssistantMessage(`ENMA ERROR · ${error.message}`, []);
      }
    } finally {
      abortRef.current = null;
      setLoading(false);
    }
  }

  // M3 task lifecycle: run a Cancel/Retry/Approve/Deny action from a
  // TaskCard, then refresh the task state so the card shows the new
  // status. Approve records the approval decision and then actually
  // resumes execution through the task runner.
  async function handleTaskAction(action, taskId, approvalId = null) {
    if (action === "cancel") {
      await taskService.cancelTask(taskId);
    } else if (action === "retry") {
      await taskService.retryTask(taskId);
    } else if (action === "approve" || action === "deny") {
      if (!approvalId) {
        throw new Error("No pending approval record for this task.");
      }
      await taskService.decideApproval(approvalId, action === "approve");
      if (action === "approve") {
        await taskService.resumeTask(taskId);
      }
    } else {
      throw new Error(`Unknown task action: ${action}`);
    }

    // Refresh through the existing GET endpoint; the TaskCard
    // re-renders from the updated message state.
    const refreshed = await taskService.getTask(taskId);

    setMessages((prev) =>
      prev.map((item) => {
        if (
          item.role === "assistant" &&
          item.taskData &&
          (item.taskData.task?.id === taskId || item.taskData.id === taskId)
        ) {
          return {
            ...item,
            taskData: {
              ...item.taskData,
              task: {
                ...item.taskData.task,
                status: refreshed.status ?? item.taskData.task?.status,
                result: refreshed.result ?? item.taskData.task?.result,
                error: refreshed.error ?? item.taskData.task?.error,
              },
              execution: refreshed.execution || {
                ...item.taskData.execution,
                state: refreshed.status,
              },
            },
          };
        }
        return item;
      }),
    );
  }

  function updateAssistantMessage(content, pages, taskData = null) {    setMessages((prev) => {
      const updated = [...prev];
      for (let i = updated.length - 1; i >= 0; i--) {
        if (updated[i].role === "assistant") {
          updated[i] = { ...updated[i], content, pages, taskData };
          break;
        }
      }
      return updated;
    });
  }

  function handleKeyDown(event) {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      sendMessage();
    }
  }

  function handleNodeClick(node) {
    setSelectedNode(node);
    setInspectorTaskDetail(null);

    if (!node) return;

    // Enrich the inspector with real data through the
    // authenticated API layer. Memory content is resolved
    // through the memory API; task priority/result come
    // from the task detail endpoint.
    if (node.type === "memory") {
      loadMemories();
    } else if (node.type === "task") {
      const taskId = node.task_id || String(node.id).replace(/^task-/, "");

      if (taskId) {
        graphService
          .fetchTaskDetail(taskId)
          .then((detail) => setInspectorTaskDetail(detail))
          .catch((error) => {
            console.error("GHOST task detail error:", error);
            setInspectorTaskDetail(null);
          });
      }
    }
  }

  // Left navigation: Mind and Chat are full workspaces; Tasks,
  // Documents and Memory open their knowledge drawers over the
  // current workspace. Skills/Settings render disabled (no views).
  function handleTabChange(tab) {
    setActiveTab(tab);

    if (tab === "memory") {
      setActiveDrawer("memory");
      loadMemories();
    } else if (tab === "documents") {
      setActiveDrawer("documents");
      loadDocuments();
    } else if (tab === "tasks") {
      setActiveDrawer("tasks");
    } else {
      setActiveDrawer(null);
    }
  }

  function clearDocument() {
    setDocumentId(null);
    setDocumentName("");
    setUploadStatus("");
    localStorage.removeItem("ghost-active-document-v2");
    loadGraph();
  }

  function newChat() {
    if (loading) return;
    setMessages([]);
    setMessage("");
    setActiveTab("chat");
  }

  // Memoized values
  const visualGraph = useMemo(() => {
    const storedLayout = readJSON("ghost-spatial-layout-v2", {});
    return buildVisualGraph(graphData, storedLayout, ZONES);
  }, [graphData]);

  const nodeCounts = useMemo(() => {
    const counts = {
      document: 0,
      memory: 0,
      project: 0,
      task: 0,
      tool: 0,
      system: 0,
    };

    graphData.nodes.forEach((node) => {
      const type = node.type || "system";
      if (counts[type] !== undefined) {
        counts[type] += 1;
      }
    });

    return counts;
  }, [graphData]);

  // Keep the inspector in sync with the latest graph poll:
  // resolve the selected node against the current visual
  // graph so refreshed data (status, counts) is reflected
  // without re-clicking.
  const inspectorNode = useMemo(() => {
    if (!selectedNode) return null;
    return (
      visualGraph.nodes.find((node) => node.id === selectedNode.id) ||
      selectedNode
    );
  }, [selectedNode, visualGraph]);

  // Real edges touching the selected node, resolved to
  // { id, label, type, direction } for the inspector.
  const inspectorConnections = useMemo(() => {
    if (!inspectorNode) return [];

    const labelById = new Map(
      visualGraph.nodes.map((node) => [node.id, node.label || node.id])
    );

    const connections = [];

    visualGraph.links.forEach((link) => {
      const sourceId =
        typeof link.source === "object" ? link.source.id : link.source;
      const targetId =
        typeof link.target === "object" ? link.target.id : link.target;

      if (sourceId === inspectorNode.id) {
        connections.push({
          id: targetId,
          label: labelById.get(targetId) || String(targetId),
          type: link.type,
          direction: "out",
        });
      } else if (targetId === inspectorNode.id) {
        connections.push({
          id: sourceId,
          label: labelById.get(sourceId) || String(sourceId),
          type: link.type,
          direction: "in",
        });
      }
    });

    return connections;
  }, [inspectorNode, visualGraph]);

  const inspectorMemory = useMemo(() => {
    if (!selectedNode || !memoryItems.length) return null;
    return resolveMemoryNode(selectedNode, memoryItems);
  }, [selectedNode, memoryItems]);

  // Render based on auth status
  if (authStatus === "checking") {
    return (
      <main
        style={{
          minHeight: "100vh",
          background: "#02070b",
          color: "#d9f7ff",
          display: "grid",
          placeItems: "center",
          fontFamily: "Inter, Segoe UI, sans-serif",
          letterSpacing: "0.16em",
        }}
      >
        VERIFYING ENMA SESSION...
      </main>
    );
  }

  if (authStatus !== "authenticated") {
    return (
      <main
        style={{
          minHeight: "100vh",
          background: "#02070b",
          color: "#d9f7ff",
          display: "grid",
          placeItems: "center",
          padding: "24px",
          fontFamily: "Inter, Segoe UI, sans-serif",
          position: "relative",
          overflow: "hidden",
        }}
      >
        <div
          style={{
            position: "absolute",
            inset: 0,
            backgroundImage: "linear-gradient(rgba(0,229,255,0.035) 1px, transparent 1px), linear-gradient(90deg, rgba(0,229,255,0.035) 1px, transparent 1px)",
            backgroundSize: "42px 42px",
            pointerEvents: "none",
          }}
        />
        <section
          style={{
            width: "min(420px, 100%)",
            border: "1px solid rgba(0,229,255,0.28)",
            background: "rgba(3,12,18,0.92)",
            boxShadow: "0 0 50px rgba(0,229,255,0.08)",
            padding: "34px",
            position: "relative",
            zIndex: 1,
          }}
        >
          <div
            style={{
              color: "#00e5ff",
              fontSize: "12px",
              letterSpacing: "0.28em",
              marginBottom: "12px",
            }}
          >
            ENMA // ACCESS
          </div>
          <h1
            style={{
              margin: 0,
              fontSize: "42px",
              letterSpacing: "0.18em",
              fontWeight: 500,
            }}
          >
            E.N.M.A.
          </h1>
          <p
            style={{
              color: "rgba(190,225,238,0.68)",
              lineHeight: 1.6,
              margin: "12px 0 28px",
              fontSize: "13px",
            }}
          >
            PERSONAL AI OPERATING SYSTEM
            <br />
            {setupMode
              ? "FIRST RUN — CREATE YOUR ENMA PASSPHRASE"
              : "AUTHENTICATED SESSION REQUIRED"}
          </p>
          {setupMode ? (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              completeSetup();
            }}
          >
            <label
              style={{
                display: "block",
                fontSize: "11px",
                letterSpacing: "0.18em",
                color: "rgba(190,225,238,0.65)",
                marginBottom: "8px",
              }}
            >
              CREATE ENMA PASSPHRASE
            </label>
            <input
              type="password"
              value={setupPassphrase}
              onChange={(event) => {
                setSetupPassphrase(event.target.value);
                setSetupError("");
              }}
              autoFocus
              autoComplete="new-password"
              placeholder="At least 8 characters"
              disabled={setupLoading}
              style={{
                width: "100%",
                boxSizing: "border-box",
                background: "rgba(0,0,0,0.28)",
                border: "1px solid rgba(0,229,255,0.22)",
                color: "#eaffff",
                padding: "14px 15px",
                outline: "none",
                fontSize: "14px",
                marginBottom: "14px",
              }}
            />
            <label
              style={{
                display: "block",
                fontSize: "11px",
                letterSpacing: "0.18em",
                color: "rgba(190,225,238,0.65)",
                marginBottom: "8px",
              }}
            >
              CONFIRM PASSPHRASE
            </label>
            <input
              type="password"
              value={setupConfirm}
              onChange={(event) => {
                setSetupConfirm(event.target.value);
                setSetupError("");
              }}
              autoComplete="new-password"
              placeholder="Repeat passphrase"
              disabled={setupLoading}
              style={{
                width: "100%",
                boxSizing: "border-box",
                background: "rgba(0,0,0,0.28)",
                border: "1px solid rgba(0,229,255,0.22)",
                color: "#eaffff",
                padding: "14px 15px",
                outline: "none",
                fontSize: "14px",
              }}
            />
            {setupError && (
              <div
                style={{
                  marginTop: "12px",
                  color: "#ff7d8d",
                  fontSize: "12px",
                  lineHeight: 1.5,
                }}
              >
                {setupError}
              </div>
            )}
            <button
              type="submit"
              disabled={
                setupLoading ||
                !setupPassphrase.trim() ||
                !setupConfirm.trim()
              }
              style={{
                width: "100%",
                marginTop: "18px",
                padding: "13px 16px",
                background: "linear-gradient(135deg, rgba(0,229,255,0.18), rgba(0,229,255,0.06))",
                border: "1px solid rgba(0,229,255,0.4)",
                borderRadius: "0",
                color: "#00e5ff",
                fontSize: "13px",
                letterSpacing: "0.2em",
                cursor: "pointer",
              }}
            >
              {setupLoading ? "SAVING..." : "CREATE & ENTER ENMA"}
            </button>
          </form>
          ) : (
          <form
            onSubmit={(event) => {
              event.preventDefault();
              login();
            }}
          >
            <label
              style={{
                display: "block",
                fontSize: "11px",
                letterSpacing: "0.18em",
                color: "rgba(190,225,238,0.65)",
                marginBottom: "8px",
              }}
            >
              ENMA PASSPHRASE
            </label>
            <input
              type="password"
              value={loginPassword}
              onChange={(event) => {
                setLoginPassword(event.target.value);
                setLoginError("");
              }}
              autoFocus
              autoComplete="current-password"
              placeholder="Enter passphrase"
              disabled={loginLoading}
              style={{
                width: "100%",
                boxSizing: "border-box",
                background: "rgba(0,0,0,0.28)",
                border: "1px solid rgba(0,229,255,0.22)",
                color: "#eaffff",
                padding: "14px 15px",
                outline: "none",
                fontSize: "14px",
              }}
            />
            {loginError && (
              <div
                style={{
                  marginTop: "12px",
                  color: "#ff7d8d",
                  fontSize: "12px",
                  lineHeight: 1.5,
                }}
              >
                {loginError}
              </div>
            )}
            <button
              type="submit"
              disabled={loginLoading || !loginPassword.trim()}
              style={{
                width: "100%",
                marginTop: "18px",
                padding: "13px 16px",
                border: "1px solid rgba(0,229,255,0.45)",
                background: loginLoading
                  ? "rgba(0,229,255,0.08)"
                  : "rgba(0,229,255,0.13)",
                color: "#dffcff",
                cursor: loginLoading ? "wait" : "pointer",
                letterSpacing: "0.18em",
                fontSize: "11px",
              }}
            >
              {loginLoading ? "AUTHENTICATING..." : "ENTER ENMA"}
            </button>
          </form>
          )}
        </section>
      </main>
    );
  }

// Main layout for authenticated users
  return (
    <MainLayout
      activeTab={activeTab}
      onTabChange={handleTabChange}
      backendOnline={backendOnline}
      selectedNode={selectedNode}
      setSelectedNode={setSelectedNode}
      nodeCounts={nodeCounts}
      inspectorMemory={inspectorMemory}
      inspectorNode={inspectorNode}
      inspectorConnections={inspectorConnections}
      inspectorTaskDetail={inspectorTaskDetail}
      graphNodes={visualGraph.nodes}
      setActiveDrawer={setActiveDrawer}
      activeDrawer={activeDrawer}
      messages={messages}
      onTaskAction={handleTaskAction}
      message={message}
      setMessage={setMessage}
      loading={loading}
      uploading={uploading}
      uploadStatus={uploadStatus}
      documentId={documentId}
      documentName={documentName}
      fileInputRef={fileInputRef}
      handleFileChange={handleFileChange}
      sendMessage={sendMessage}
      stopGeneration={stopGeneration}
      handleKeyDown={handleKeyDown}
      newChat={newChat}
      clearDocument={clearDocument}
      forgetAllMemories={forgetAllMemories}
      deleteDocument={deleteDocument}
      documents={documentItems}
      documentsLoading={documentsLoading}
      memoryItems={memoryItems}
      memoryLoading={memoryLoading}
      deleteMemory={deleteMemory}
    renderGhostResponse={(text) => {
      if (!text) {
        return <div className="empty-response">Waiting for ENMA response...</div>;
      }

      const normalized = text.replace(/\r\n/g, "\n");
      const blocks = normalized.split(/(```[\s\S]*?```)/g);

      return (
        <div className="ghost-response-body">
          {blocks.map((block, blockIndex) => {
            if (block.startsWith("```")) {
              const lines = block.split("\n");
              const language = lines[0]
                .replace("```", "")
                .trim()
                .toLowerCase();
              const code = lines.slice(1, -1).join("\n");

            if (language === "mermaid" || language === "flowchart") {
              return (
                <div key={blockIndex}>
                  {/* Mermaid diagram would be rendered here */}
                  <div className="mermaid-placeholder">
                    Mermaid Diagram: {language}
                  </div>
                </div>
              );
            }

            return (
              <CodeBlock
                key={blockIndex}
                language={language}
                code={code}
              />
            );
            }

            const lines = block.split("\n");
            return (
              <div key={blockIndex} className="ghost-text-block">
                {renderTextBlock(block, String(blockIndex))}
              </div>
            );
          })}
        </div>
      );
    }}
    onLogout={logout}
    >
      {/* Main content - Neural Graph (prepared visual graph with positions) */}
      <NeuralGraph
        graphData={visualGraph}
        selectedNode={selectedNode}
        setSelectedNode={setSelectedNode}
        onNodeClick={handleNodeClick}
        onNodeDoubleClick={handleNodeClick}
        onBackgroundClick={() => setSelectedNode(null)}
        graphRef={graphRef}
      />
    </MainLayout>
  );
}

export default App;