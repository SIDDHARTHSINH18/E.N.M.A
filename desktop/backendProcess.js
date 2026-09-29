// ENMA desktop — backend process lifecycle helpers.
//
// Pure decision functions extracted from main.js so shutdown,
// ownership and startup-failure behavior are unit-testable
// without Electron and without touching real processes.
// main.js keeps the actual spawn/taskkill side effects.

/**
 * Plan how to terminate the backend ENMA owns.
 *
 * Ownership rule: ONLY the PID of the backend process this
 * instance spawned may appear in the plan. On Windows the tree
 * is killed (/T) because a "py -3" launcher makes uvicorn a
 * grandchild; other platforms use a plain kill().
 */
function planBackendTermination(pid, platform) {
  if (!pid) {
    return null;
  }

  if (platform === "win32") {
    return {
      tool: "taskkill",
      args: ["/pid", String(pid), "/T", "/F"],
      windowsHide: true,
      graceful: false,
    };
  }

  return { tool: "kill", args: [], graceful: true };
}

/**
 * Decide the backendState transition when the backend process
 * fires its exit event. Deliberate shutdown ("stopping") must
 * never be reported as a startup failure; an unexpected exit is
 * a failed startup with a truthful reason.
 */
function nextStateOnExit(state, code, signal) {
  if (state === "stopping") {
    return { state: "stopped", reason: "" };
  }

  if (state === "running") {
    return {
      state: "failed",
      reason:
        `backend process exited during startup ` +
        `(code=${code}, signal=${signal}). Check the packaged ` +
        "Python runtime and the backend configuration file in " +
        "the ENMA user-data directory.",
    };
  }

  return { state, reason: "" };
}

module.exports = {
  planBackendTermination,
  nextStateOnExit,
};
