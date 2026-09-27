import { useMemo, useState } from "react";

/**
 * Lifecycle action availability per task status:
 * PENDING/RUNNING -> Cancel, FAILED -> Retry,
 * terminal states (COMPLETED/CANCELLED) -> no action.
 */
export function lifecycleActionFor(status) {
  const key = (status || "").toUpperCase();

  if (key === "PENDING" || key === "RUNNING") return "cancel";
  if (key === "FAILED") return "retry";

  return null;
}

/**
 * Compact, structured presentation for a POST /api/tasks
 * response. The full structured object stays intact for
 * expandable details — it is never dumped as raw JSON into
 * the conversation body.
 */

const STATUS_LABELS = {
  COMPLETED: "Completed",
  RUNNING: "Running",
  PENDING: "Pending",
  FAILED: "Failed",
  CANCELLED: "Cancelled",
  TIMEOUT: "Timed out",
  PAUSED: "Waiting for approval",
  PENDING_APPROVAL: "Waiting for approval",
  WAITING_FOR_APPROVAL: "Waiting for approval",
};

function statusLabel(value) {
  const key = (value || "").toUpperCase();

  return STATUS_LABELS[key] || key || "Unknown";
}

// APPROVAL states highlight amber, failure states red,
// everything else uses the standard cyan "done" treatment.
const TONE = {
  completed: "done",
  running: "active",
  pending: "active",
  paused: "approval",
  pending_approval: "approval",
  waiting_for_approval: "approval",
  failed: "failed",
  cancelled: "failed",
  timeout: "failed",
};

function statusTone(value) {
  const key = (value || "").toLowerCase();

  return TONE[key] || "done";
}

function mark(ready) {
  return ready ? "✓" : "•";
}

/**
 * Best-effort human-readable result text from the execution
 * envelope. The API keeps its full structure; we only read.
 */
export function extractTaskResult(data) {
  const execution = data?.execution || {};

  if (typeof execution.result === "string" && execution.result.trim()) {
    return execution.result.trim();
  }

  if (typeof execution.reason === "string" && execution.reason.trim()) {
    return execution.reason.trim();
  }

  const steps = execution.steps || [];

  const stepOutputs = steps
    .map(
      (step) =>
        (typeof step.result === "string" && step.result) ||
        (typeof step.output === "string" && step.output) ||
        ""
    )
    .filter(Boolean);

  if (stepOutputs.length > 0) {
    return stepOutputs.join("\n\n");
  }

  return "";
}

export default function TaskCard({ task, onTaskAction }) {
  const sections = useMemo(() => {
    const execution = task?.execution || {};
    const planning = task?.planning || {};
    const taskInfo = task?.task || {};

    const status =
      execution.state ||
      execution.status ||
      taskInfo.status ||
      task?.status ||
      "";

    const steps = execution.steps || planning.steps || [];

    const assumptions = planning.assumptions || [];

    return {
      title: taskInfo.title || planning.request || "",
      status,
      taskId: taskInfo.id || task?.id || null,
      planningReady: Boolean(planning.ready),
      planningSteps: steps.length,
      assumptions,
      approvalId: execution.approval_id || null,
      result: extractTaskResult(task),
    };
  }, [task]);

  const [actionBusy, setActionBusy] = useState(false);
  const [actionError, setActionError] = useState(null);

  if (!task) {
    return null;
  }

  const tone = statusTone(sections.status);
  const action = lifecycleActionFor(sections.status);

  const runLifecycleAction = async () => {
    if (!action || !sections.taskId || actionBusy || !onTaskAction) {
      return;
    }

    setActionBusy(true);
    setActionError(null);

    try {
      await onTaskAction(action, sections.taskId);
    } catch (error) {
      setActionError(
        error?.message
          ? `${action === "cancel" ? "Cancel" : "Retry"} failed: ${error.message}`
          : `${action === "cancel" ? "Cancel" : "Retry"} failed.`,
      );
    } finally {
      setActionBusy(false);
    }
  };

  const actionLabel = action === "cancel" ? "Cancel" : "Retry";

  return (
    <div className={`task-card task-card-${tone}`}>
      <div className="task-card-header">
        <span className="task-card-badge">
          {mark(tone === "done")}
        </span>
        <div className="task-card-titles">
          <div className="task-card-title">
            {sections.title || "Task"}
          </div>
          <div className="task-card-status">
            {statusLabel(sections.status)}
          </div>
        </div>
      </div>

      <div className="task-card-grid">
        <div className="task-card-section">
          <div className="task-card-section-title">TASK</div>
          <div className="task-card-section-body">
            {mark(sections.status === "COMPLETED")}{" "}
            {statusLabel(sections.status)}
          </div>
        </div>

        <div className="task-card-section">
          <div className="task-card-section-title">PLANNING</div>
          <div className="task-card-section-body">
            {mark(sections.planningReady)}{" "}
            {sections.planningReady
              ? `Ready · ${sections.planningSteps} step${
                  sections.planningSteps === 1 ? "" : "s"
                }`
              : "Pending"}
          </div>
          {sections.assumptions.length > 0 && (
            <div className="task-card-assumptions">
              {sections.assumptions
                .slice(0, 3)
                .map((assumption, index) => (
                  <div
                    className="task-card-assumption"
                    key={index}
                  >
                    ◆ {String(assumption)}
                  </div>
                ))}
            </div>
          )}
        </div>
      </div>

      {sections.result && (
        <div className="task-card-section task-card-result-wrap">
          <div className="task-card-section-title">RESULT</div>
          <div className="task-card-result">
            {sections.result}
          </div>
        </div>
      )}

      {sections.approvalId && (
        <div className="task-card-note">
          Approval required · id {sections.approvalId}
        </div>
      )}

      {action && sections.taskId && (
        <button
          className="task-card-lifecycle-btn"
          onClick={runLifecycleAction}
          disabled={actionBusy}
          aria-label={`${actionLabel} task`}
        >
          {actionBusy
            ? actionLabel === "Cancel"
              ? "Cancelling…"
              : "Retrying…"
            : actionLabel}
        </button>
      )}

      {actionError && (
        <div className="task-card-note task-card-error">{actionError}</div>
      )}

      <details className="task-card-details">
        <summary>Details</summary>
        <pre className="task-card-json">
          {JSON.stringify(task, null, 2)}
        </pre>
      </details>
    </div>
  );
}
