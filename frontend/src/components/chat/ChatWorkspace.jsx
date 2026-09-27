import { useEffect, useRef } from "react";
import { formatSourcePages } from "../../utils/helpers";
import TaskCard from "./TaskCard";

/**
 * ChatWorkspace - the dedicated conversation workspace.
 *
 * Renders the real message thread (user + streamed GHOST
 * responses) inside the main workspace area. The graph is not
 * required here; returning to Mind restores it unchanged.
 */
export default function ChatWorkspace({
  messages = [],
  loading = false,
  contextNode = null,
  onClearContext = () => {},
  onNewChat = () => {},
  onTaskAction = null,
  renderGhostResponse = () => null,
}) {
  const threadRef = useRef(null);

  // Keep the newest message in view while streaming.
  useEffect(() => {
    if (threadRef.current) {
      threadRef.current.scrollTop = threadRef.current.scrollHeight;
    }
  }, [messages, loading]);

  const visibleMessages = messages.filter(
    (item) =>
      (item.role === "user" || item.role === "assistant") &&
      (item.content || item.role === "user")
  );

  return (
    <div className="chat-workspace">
      <div className="chat-header">
        <span className="chat-header-label">Conversation</span>
        <button
          className="chat-new"
          onClick={onNewChat}
          disabled={loading}
          title="Start a new conversation"
        >
          + New chat
        </button>
      </div>

      <div className="chat-thread" ref={threadRef}>
        {visibleMessages.length === 0 && !loading && (
          <div className="chat-empty">
            <div className="chat-empty-title">ENMA</div>
            <p className="chat-empty-hint">
              Your second brain. Ask anything, or go back to the Mind to
              explore your knowledge graph.
            </p>
            {contextNode && (
              <div className="chat-context-hint">
                Selected in Mind: <b>{contextNode.label}</b>
              </div>
            )}
          </div>
        )}

        {visibleMessages.map((item, index) =>
          item.role === "user" ? (
            <div className="chat-msg chat-msg-user" key={index}>
              <div className="chat-msg-content">{item.content}</div>
            </div>
          ) : (
            <div className="chat-msg chat-msg-ghost" key={index}>
              <div className="chat-msg-meta">ENMA</div>
              <div className="chat-msg-content">
                {item.content
                  ? renderGhostResponse(item.content)
                  : <span className="chat-streaming">▍</span>}
                {item.taskData && (
                  <TaskCard
                    task={item.taskData}
                    onTaskAction={onTaskAction}
                  />
                )}
                {item.pages && item.pages.length > 0 && (
                  <div className="chat-msg-sources">
                    <span>SOURCES</span>
                    {formatSourcePages(item.pages).map((range) => (
                      <span className="source-page" key={range}>
                        {range}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            </div>
          )
        )}

        {loading && (
          <div className="chat-msg chat-msg-ghost">
            <div className="chat-msg-meta">ENMA</div>
            <div className="chat-msg-content">
              <span className="chat-streaming">thinking…</span>
            </div>
          </div>
        )}
      </div>

      {contextNode && (
        <button
          className="chat-context-chip"
          onClick={onClearContext}
          title="Clear the selected graph context for this conversation"
        >
          <span className="chat-context-dot" />
          Context: {contextNode.label}
          <span className="chat-context-clear">×</span>
        </button>
      )}
    </div>
  );
}
