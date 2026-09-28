const { app, BrowserWindow } = require("electron");
const path = require("path");
const http = require("http");
const net = require("net");
const fs = require("fs");
const { spawn, execFile } = require("child_process");
const crypto = require("crypto");

let backendProcess = null;
let staticServer = null;
let staticPort = null;

// The renderer only ever talks to loopback services, so system
// proxy auto-detection (WPAD/PAC) must never delay its first
// request: on misconfigured networks that resolution alone can
// stall Chromium for minutes before any window content loads.
// Single instance: a second ENMA.exe cannot own the strict
// frontend port (5177) and would fall back to a CORS-broken
// file:// UI. Focus the existing window instead.
const gotSingleInstanceLock = app.requestSingleInstanceLock();

if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    const [win] = BrowserWindow.getAllWindows();
    if (win) {
      if (win.isMinimized()) {
        win.restore();
      }
      win.focus();
    }
  });
}

app.commandLine.appendSwitch("no-proxy-server");

// Deterministic ENMA development ports. The frontend static
// port is STRICT: if it is occupied, ENMA reports the conflict
// instead of silently moving to another port. Both values are
// in the backend CORS allowlist (backend/main.py).
const BACKEND_HOST = "127.0.0.1";
const BACKEND_PORT = 8000;
const FRONTEND_PORT = 5177;

const MIME_TYPES = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".ico": "image/x-icon",
  ".json": "application/json; charset=utf-8",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
};

// In a packaged build the frontend bundle and the backend
// source tree are copied by electron-builder into
// <install>/resources/ (see "extraResources" in package.json).
// In development they stay as sibling directories of desktop/.
function frontendDistPath() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "frontend", "dist");
  }
  return path.join(__dirname, "..", "frontend", "dist");
}

function backendProjectRoot() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "backend-project");
  }
  return path.join(__dirname, "..");
}

// Serve the bundle over loopback HTTP instead of file://: a
// file:// page sends Origin: null, which the backend CORS
// allowlist rejects, so login and every API call would fail.
function startStaticServer(distRoot) {
  return new Promise((resolve) => {
    const start = () => {
      const server = http.createServer((req, res) => {
        const urlPath = decodeURIComponent(
          (req.url || "/").split("?")[0]
        );

        // Desktop-layer diagnostics (metadata only, no secrets):
        // lets the renderer tell a dead backend from an expired
        // session.
        if (urlPath === "/enma-backend-status") {
          res.writeHead(200, {
            "Content-Type": "application/json; charset=utf-8"
          });

          const configPath = path.join(
            app.getPath("userData"),
            "config.env"
          );

          let setupPending = false;

          try {
            setupPending = fs
              .readFileSync(configPath, "utf-8")
              .split(/\r?\n/)
              .some((line) => line === "ENMA_SETUP_PENDING=1");
          } catch {
            // No config yet — nothing pending to report.
          }

          res.end(JSON.stringify({
            backend: backendState,
            reason: backendReason,
            port: `${BACKEND_HOST}:${BACKEND_PORT}`,
            backendPid: backendProcess ? backendProcess.pid : null,
            configPath,
            setupPending
          }));
          return;
        }

        const relative =
          urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");

        const filePath = path.resolve(distRoot, relative);

        if (!filePath.startsWith(distRoot + path.sep)) {
          res.writeHead(403);
          res.end();
          return;
        }

        fs.readFile(filePath, (error, content) => {
          if (error) {
            res.writeHead(404);
            res.end();
            return;
          }

          const type =
            MIME_TYPES[path.extname(filePath).toLowerCase()] ||
            "application/octet-stream";

          res.writeHead(200, { "Content-Type": type });
          res.end(content);
        });
      });

      server.on("error", (error) => {
        // STRICT PORT: never fall back to another port silently.
        console.error(
          `ENMA frontend port ${FRONTEND_PORT} is unavailable ` +
          `(${error.code || error.message}). Close whatever is ` +
          "using it and restart ENMA."
        );
        resolve(null);
      });

      server.listen(FRONTEND_PORT, "127.0.0.1", () => {
        staticServer = server;
        staticPort = FRONTEND_PORT;
        resolve(staticPort);
      });
    };

    start();
  });
}

// ------------------------------------------------------------
// Packaged runtime resolution + first-run configuration.
// ------------------------------------------------------------

let backendState = "not-started";
let backendReason = "";

// Candidates are validated by actually importing the backend's
// required modules — a name on PATH is not proof of a working
// runtime. The packaged embedded runtime is preferred (ENMA is
// self-contained); an explicit ENMA_PYTHON still wins over the
// PATH fallbacks for diagnostics/development, but not over the
// bundled runtime an installed ENMA ships with.
function embeddedPythonPath() {
  if (!app.isPackaged) {
    return null;
  }
  return path.join(process.resourcesPath, "runtime", "python.exe");
}

function validatePython(candidate) {
  // "python" or ["py", "-3"] — both normalized here.
  const [file, ...prefix] = Array.isArray(candidate)
    ? candidate
    : [candidate];

  return new Promise((resolve) => {
    execFile(
      file,
      [...prefix, "-c", "import fastapi, uvicorn, dotenv"],
      { timeout: 20000 },
      (error) => resolve(!error)
    );
  });
}

// Best-effort check for the ML retrieval stack. The backend
// starts and serves without it (semantic retrieval is lazy),
// so a miss is a loud warning, not a startup failure.
function warnIfMlStackMissing(candidate) {
  const [file, ...prefix] = Array.isArray(candidate)
    ? candidate
    : [candidate];

  execFile(
    file,
    [...prefix, "-c", "import sentence_transformers"],
    { timeout: 60000 },
    (error) => {
      if (error) {
        console.warn(
          "ENMA embedded runtime: sentence_transformers is not " +
          "importable; document/semantic retrieval will be " +
          "unavailable. All other backend features are unaffected."
        );
      }
    }
  );
}

async function resolvePython() {
  const candidates = [];

  const embedded = embeddedPythonPath();
  if (embedded) {
    if (!fs.existsSync(embedded)) {
      console.error(
        `ENMA embedded runtime missing at ${embedded}; ` +
        "falling back to the system Python. The installed " +
        "application may be incomplete — reinstall ENMA."
      );
    } else {
      candidates.push(embedded);
    }
  }

  if (process.env.ENMA_PYTHON) {
    candidates.push(process.env.ENMA_PYTHON);
  }
  candidates.push("python", "py -3");

  for (const candidate of candidates) {
    const parts = candidate.split(" ");
    const ok = await validatePython(parts);
    if (ok) {
      console.log(`ENMA backend runtime: ${candidate}`);
      if (candidate === embedded) {
        warnIfMlStackMissing(parts);
      }
      return parts;
    }
    console.warn(
      `ENMA backend runtime candidate not usable: ${candidate}`
    );
  }

  return null;
}

// First-run configuration: the backend refuses to start without
// GHOST_AUTH_PASSWORD, so the desktop layer generates a strong
// one into the user's private config file. The user may edit
// that file (including adding provider keys) — it is never
// shipped, never logged, and never sent to the renderer.
function ensureBackendConfig() {
  const configPath = path.join(
    app.getPath("userData"),
    "config.env"
  );

  if (!fs.existsSync(configPath)) {
    const passphrase = crypto.randomBytes(24)
      .toString("base64url");

    fs.writeFileSync(
      configPath,
      [
        "# ENMA backend configuration (created automatically",
        "# on first run). Keep this file private.",
        "#",
        "# Login passphrase for the ENMA app:",
        `GHOST_AUTH_PASSWORD=${passphrase}`,
        "#",
        "# First-run marker: on next launch the app asks you to",
        "# create your own passphrase. This bootstrap value is",
        "# then replaced by YOUR chosen passphrase.",
        "ENMA_SETUP_PENDING=1",
        "",
        "# Provider keys are optional; without them the chat",
        "# provider reports itself unreachable:",
        "GEMINI_API_KEY=",
        "#",
        "# Optional Groq provider (fast demo provider):",
        "# GROQ_API_KEY=gsk_...",
        "",
      ].join("\n"),
      { encoding: "utf-8" }
    );

    console.log(
      `ENMA first-run configuration created at ${configPath}.`
    );
  }

  return configPath;
}

function probeBackend() {
  // Resolves "healthy" | "foreign" | "free" for 127.0.0.1:8000.
  return new Promise((resolve) => {
    const socket = net.connect(BACKEND_PORT, BACKEND_HOST);
    socket.setTimeout(1500);

    const finish = (answer) => {
      socket.destroy();
      resolve(answer);
    };

    socket.on("connect", () => {
      http
        .get(
          `http://${BACKEND_HOST}:${BACKEND_PORT}/health`,
          (res) => {
            res.resume();
            // Any HTTP response means an ENMA backend already
            // owns the port (/health is public and unauthenticated
            // by design).
            finish("healthy");
          }
        )
        .on("error", () => finish("foreign"));

      socket.setTimeout(1500, () => finish("foreign"));
    });

    socket.on("error", () => finish("free"));
    socket.on("timeout", () => finish("free"));
  });
}

// ------------------------------------------------------------
// User-writable runtime directories (packaged vs dev).
//
// The backend's storage engines default to paths derived from
// the backend source location, which inside an installed
// application is a protected Program Files directory. Their
// documented env overrides are pointed at the Electron userData
// directory here, so an installed ENMA never writes into its
// installation tree: application resources stay in resources/,
// user data lives in %APPDATA%/enma-desktop.
// ------------------------------------------------------------
function userDataDir(name) {
  const dir = path.join(app.getPath("userData"), name);
  fs.mkdirSync(dir, { recursive: true });
  return dir;
}

function backendRuntimeEnv() {
  // Development keeps the repository's own backend/data storage
  // conventions untouched; only the packaged runtime — whose
  // installation tree is not writable — is redirected.
  if (!app.isPackaged) {
    return {};
  }
  return {
    GHOST_MEMORY_PATH: path.join(
      userDataDir("data"),
      "memory.json"
    ),
    GHOST_AUDIT_PATH: path.join(
      userDataDir("data"),
      "audit.jsonl"
    ),
    GHOST_TASKS_PATH: path.join(
      userDataDir("data"),
      "tasks.jsonl"
    ),
    ENMA_WORKSPACE_ROOT: userDataDir("workspace"),
    // HuggingFace model cache (all-MiniLM-L6-v2, downloaded on
    // first semantic retrieval) must land in user-writable space,
    // never in the read-only resources/ install tree.
    HF_HOME: userDataDir("hf-cache"),
  };
}

async function startBackend() {
  // 1. Runtime: never trust an unvalidated PATH lookup.
  const runtime = await resolvePython();

  if (!runtime) {
    backendState = "no-runtime";
    backendReason =
      "No usable Python runtime with the ENMA backend " +
      "dependencies was found. Set the ENMA_PYTHON environment " +
      "variable to a Python interpreter that has fastapi, " +
      "uvicorn and python-dotenv installed.";
    console.error("ENMA BACKEND NOT STARTED. " + backendReason);
    return;
  }

  // 2. Configuration: first-run bootstrap into the user's
  //    private data directory (never the install tree).
  const configPath = ensureBackendConfig();

  // 3. Spawn with the resolved runtime, config path, and the
  //    user-writable storage/workspace directories.
  backendProcess = spawn(
    runtime[0],
    [
      ...runtime.slice(1),
      "-m",
      "uvicorn",
      "backend.main:app",
      "--host",
      BACKEND_HOST,
      "--port",
      String(BACKEND_PORT),
    ],
    {
      cwd: backendProjectRoot(),
      windowsHide: true,
      env: {
        ...process.env,
        ENMA_CONFIG_PATH: configPath,
        ...backendRuntimeEnv(),
      },
    }
  );

  backendState = "running";
  backendReason = "";

  // Metadata diagnostics (never secrets): which executable and
  // which config file this backend instance is tied to.
  console.log(
    `ENMA app exe: ${app.getPath("exe")}`
  );
  console.log(
    `ENMA backend pid: ${backendProcess.pid}; ` +
    `backend config: ${configPath}`
  );

  backendProcess.on("error", (error) => {
    backendState = "failed";
    backendReason = `backend process error: ${error.code || error.message}`;
    console.error(
      "ENMA BACKEND FAILED TO START. " +
      `Port: ${BACKEND_HOST}:${BACKEND_PORT}. ` +
      `Reason: ${backendReason}`
    );
  });

  backendProcess.on("exit", (code, signal) => {
    if (backendState === "running") {
      backendState = "failed";
      backendReason =
        `backend process exited during startup ` +
        `(code=${code}, signal=${signal}). Check the packaged ` +
        "Python runtime and the backend configuration file in " +
        "the ENMA user-data directory.";
      console.error(
        "ENMA BACKEND EXITED. " +
        `Port: ${BACKEND_HOST}:${BACKEND_PORT}. ` +
        `Reason: ${backendReason}`
      );
    }
    backendProcess = null;
  });
}

function stopBackend() {
  if (!backendProcess) {
    return;
  }

  // On Windows the backend may be launched through the "py -3"
  // launcher, so the real uvicorn runs as a child of the direct
  // child process. A plain kill() would orphan that grandchild
  // and leave it holding port 8000 after ENMA exits. Kill the
  // whole process tree instead; other platforms keep kill().
  // Mark intent first: the exit handler must not report this
  // deliberate shutdown as a backend startup failure.
  backendState = "stopping";
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(backendProcess.pid), "/T", "/F"], {
      windowsHide: true,
    });
  } else {
    backendProcess.kill();
  }

  backendProcess = null;
}

function createWindow() {
  const win = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 1100,
    minHeight: 700,
    show: false,
    webPreferences: {
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  let shown = false;

  const show = () => {
    if (shown) return;
    shown = true;
    win.show();
  };

  win.once("ready-to-show", show);

  // The window must open promptly even when the backend or the
  // provider is unavailable, so first paint must never be the
  // only thing that can reveal it: bounded fallback plus an
  // explicit failure path instead of an invisible app.
  setTimeout(show, 2000);

  win.webContents.once("did-fail-load", () => {
    console.error("ENMA frontend failed to load");
    show();
  });

  if (staticPort) {
    win.loadURL(`http://127.0.0.1:${staticPort}/`);
  } else {
    win.loadFile(path.join(frontendDistPath(), "index.html"));
  }
}

app.whenReady().then(async () => {
  const state = await probeBackend();

  if (state === "healthy") {
    // A current ENMA backend already owns 127.0.0.1:8000 —
    // reuse it instead of spawning a process that cannot bind.
    console.log(
      `ENMA backend already running on ${BACKEND_HOST}:${BACKEND_PORT}; reusing it.`
    );
  } else if (state === "foreign") {
    // STRICT PORT: never move the backend; report the conflict.
    console.error(
      `PORT CONFLICT: ${BACKEND_HOST}:${BACKEND_PORT} is occupied by ` +
        "another application. ENMA will not start its backend on a " +
        "different port. Free the port and restart ENMA."
    );
  } else {
    await startBackend();
  }

  await startStaticServer(frontendDistPath());

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) {
      createWindow();
    }
  });
});

app.on("before-quit", () => {
  stopBackend();

  if (staticServer) {
    staticServer.close();
    staticServer = null;
  }
});

app.on("window-all-closed", () => {
  if (process.platform !== "darwin") {
    app.quit();
  }
});
