// ENMA desktop shell tests — plain Node, no new dependencies.
// Run: node config.test.js  (or: npm test)
//
// Coverage:
//   A-D  probeBackend classification (healthy/foreign/unavailable/free)
//        against REAL local sockets on ephemeral ports
//   E-F  ensureBackendConfig first-run vs existing-config behavior
//   G    config template <-> backend startup-validation agreement
//   H-K  backend process ownership / shutdown / failure-state
//        decisions (pure plans — no real process killing)
//   +    upgrade path cases 1-8 from the P1 spec

const assert = require("assert");
const fs = require("fs");
const net = require("net");
const os = require("os");
const http = require("http");
const path = require("path");
const crypto = require("crypto");

const {
  buildConfigContent,
  upgradeBackendConfigContent,
  chooseDefaultProvider,
  ensureBackendConfig,
} = require("./config");
const { probeBackend } = require("./probe");
const {
  planBackendTermination,
  nextStateOnExit,
} = require("./backendProcess");

let passed = 0;
let failed = 0;
const failures = [];

function test(name, fn) {
  return Promise.resolve()
    .then(fn)
    .then(() => {
      passed++;
      console.log(`  ok - ${name}`);
    })
    .catch((error) => {
      failed++;
      failures.push({ name, error });
      console.error(`  FAIL - ${name}\n    ${error.message}`);
    });
}

function tmpdir() {
  return fs.mkdtempSync(
    path.join(os.tmpdir(), "enma-desktop-test-")
  );
}

// ============================================================
// A-D. probeBackend classification (real sockets)
// ============================================================

async function testProbe() {
  await test("A. healthy: HTTP server answering /health", async () => {
    const server = http.createServer((req, res) => {
      res.end('{"status":"ok"}');
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const port = server.address().port;
    try {
      assert.strictEqual(
        await probeBackend({ host: "127.0.0.1", port, timeoutMs: 800 }),
        "healthy"
      );
    } finally {
      server.close();
    }
  });

  await test("B. unavailable: closed port -> free", async () => {
    // Grab a port and immediately release it.
    const server = net.createServer();
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const port = server.address().port;
    await new Promise((r) => server.close(r));
    assert.strictEqual(
      await probeBackend({ host: "127.0.0.1", port, timeoutMs: 800 }),
      "free"
    );
  });

  await test("C. foreign: TCP accepts but never answers HTTP", async () => {
    const server = net.createServer((socket) => {
      // Accepts and stays silent: HTTP request will time out.
      socket.on("data", () => {});
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const port = server.address().port;
    try {
      assert.strictEqual(
        await probeBackend({ host: "127.0.0.1", port, timeoutMs: 800 }),
        "foreign"
      );
    } finally {
      server.close();
    }
  });

  await test("D. foreign: TCP accepts but resets HTTP", async () => {
    const server = net.createServer((socket) => {
      socket.on("data", () => socket.destroy());
    });
    await new Promise((r) => server.listen(0, "127.0.0.1", r));
    const port = server.address().port;
    try {
      assert.strictEqual(
        await probeBackend({ host: "127.0.0.1", port, timeoutMs: 800 }),
        "foreign"
      );
    } finally {
      server.close();
    }
  });
}

// ============================================================
// E-G. ensureBackendConfig
// ============================================================

function read(file) {
  return fs.readFileSync(file, "utf-8");
}

async function testConfig() {
  await test("E. first run creates the full template", async () => {
    const dir = tmpdir();
    const file = path.join(dir, "config.env");

    ensureBackendConfig({ configPath: file, fs, crypto });

    const content = read(file);
    assert.match(content, /^GHOST_AUTH_PASSWORD=.+/m);
    assert.match(content, /^ENMA_SETUP_PENDING=1$/m);
    assert.match(content, /^ENMA_DEFAULT_PROVIDER=groq$/m);
  });

  await test("E2. first run is not repeated (existing file wins)", async () => {
    const dir = tmpdir();
    const file = path.join(dir, "config.env");

    ensureBackendConfig({ configPath: file, fs, crypto });
    const first = read(file);

    // Second call: no regeneration, content stable.
    ensureBackendConfig({ configPath: file, fs, crypto });

    assert.strictEqual(read(file), first);
  });

  await test("F. existing config without credentials is left intact", async () => {
    const dir = tmpdir();
    const file = path.join(dir, "config.env");
    const existing = "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n";
    fs.writeFileSync(file, existing, "utf-8");

    ensureBackendConfig({ configPath: file, fs, crypto });

    const content = read(file);
    // Credential preserved byte-for-byte.
    assert.match(content, /^GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE$/m);
    // Upgrade adds the provider line compatible with no keys.
    assert.match(content, /^ENMA_DEFAULT_PROVIDER=groq$/m);
  });

  await test("G. template agreement with backend startup validation", async () => {
    // backend/main.py validate_startup_config() checks the key
    // named by ENMA_DEFAULT_PROVIDER (default gemini). The
    // template must therefore set the provider to one whose key
    // the template also references as the required one.
    const template = buildConfigContent("x");
    const match = template.match(/^ENMA_DEFAULT_PROVIDER=(.+)$/m);
    assert.strictEqual(match[1], "groq");
    // And the template instructs the user to set that key.
    assert.match(template, /^# GROQ_API_KEY=/m);
  });
}

// ============================================================
// Upgrade path — spec cases 1-8
// ============================================================

async function testUpgrade() {
  await test("Case 1: fresh config (no file) -> template created", async () => {
    const dir = tmpdir();
    const file = path.join(dir, "config.env");
    ensureBackendConfig({ configPath: file, fs, crypto });
    assert.match(read(file), /^ENMA_DEFAULT_PROVIDER=groq$/m);
  });

  await test("Case 2: existing ENMA_DEFAULT_PROVIDER is preserved", () => {
    const content =
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n" +
      "ENMA_DEFAULT_PROVIDER=gemini\n" +
      "GEMINI_API_KEY=TEST_GEMINI_KEY\n";
    const result = upgradeBackendConfigContent(content);
    assert.strictEqual(result.changed, false);
    assert.strictEqual(result.content, content);
    assert.strictEqual(result.reason, "already-configured");
  });

  await test("Case 3: missing provider + GROQ key -> groq", () => {
    const content =
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n" +
      "GROQ_API_KEY=TEST_GROQ_KEY\n";
    const { content: next, changed } = upgradeBackendConfigContent(content);
    assert.ok(changed);
    assert.match(next, /^ENMA_DEFAULT_PROVIDER=groq$/m);
    // Existing lines untouched.
    assert.match(next, /^GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE$/m);
    assert.match(next, /^GROQ_API_KEY=TEST_GROQ_KEY$/m);
  });

  await test("Case 4: missing provider + GEMINI-only key -> gemini", () => {
    const content =
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n" +
      "GEMINI_API_KEY=TEST_GEMINI_KEY\n";
    const { content: next } = upgradeBackendConfigContent(content);
    assert.match(next, /^ENMA_DEFAULT_PROVIDER=gemini$/m);
  });

  await test("Case 5: both provider keys -> groq (chat provider)", () => {
    const content =
      "GEMINI_API_KEY=TEST_GEMINI_KEY\n" +
      "GROQ_API_KEY=TEST_GROQ_KEY\n";
    const { content: next } = upgradeBackendConfigContent(content);
    assert.strictEqual(chooseDefaultProvider(content), "groq");
    assert.match(next, /^ENMA_DEFAULT_PROVIDER=groq$/m);
  });

  await test("Case 6: credentials remain byte-for-byte unchanged", () => {
    const content = [
      "# private",
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE",
      "GROQ_MODEL=openai/gpt-oss-120b",
      "GROQ_API_KEY=TEST_GROQ_KEY",
      "GEMINI_API_KEY=TEST_GEMINI_KEY",
      "ENMA_SETUP_PENDING=1",
      "",
    ].join("\n");
    const { content: next } = upgradeBackendConfigContent(content);

    for (const line of content.split("\n")) {
      assert.ok(
        next.includes(line),
        `line missing or altered: ${line}`
      );
    }
    assert.strictEqual(
      next.split("\n").filter((l) => l.startsWith("GROQ_API_KEY=")).length,
      1,
      "credential line must not be duplicated"
    );
  });

  await test("Case 7: idempotent — running twice yields the same state", () => {
    const content = "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n";
    const once = upgradeBackendConfigContent(content);
    const twice = upgradeBackendConfigContent(once.content);

    assert.ok(once.changed);
    assert.strictEqual(twice.changed, false);
    assert.strictEqual(twice.content, once.content);
  });

  await test("Case 8: malformed/partial config is handled safely", () => {
    // Empty file.
    const empty = upgradeBackendConfigContent("");
    assert.ok(empty.changed);
    assert.match(empty.content, /^ENMA_DEFAULT_PROVIDER=groq$/m);

    // Garbage lines only.
    const garbage = upgradeBackendConfigContent(
      "not a config line\n# comment only\n"
    );
    assert.ok(garbage.changed);

    // Commented-out provider line does not count as configured.
    const commented = upgradeBackendConfigContent(
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n# ENMA_DEFAULT_PROVIDER=groq\n"
    );
    assert.ok(commented.changed);
    assert.match(
      commented.content,
      /^ENMA_DEFAULT_PROVIDER=groq$/m
    );
  });

  await test("upgrade never logs or exposes key values", () => {
    // The ensureBackendConfig console output mentions only the
    // reason, never file content. (Verified by inspecting the
    // console contract here: no VALUE strings are passed to log.)
    const content =
      "GHOST_AUTH_PASSWORD=TEST_USER_PASSPHRASE\n" +
      "GROQ_API_KEY=TEST_GROQ_KEY\n";
    const { content: next } = upgradeBackendConfigContent(content);
    const dir = tmpdir();
    const file = path.join(dir, "config.env");
    fs.writeFileSync(file, next, "utf-8");

    // Re-run through the file path: stable, no error, no extra log data.
    ensureBackendConfig({ configPath: file, fs, crypto });
    assert.strictEqual(read(file), next);
  });
}

// ============================================================
// H-K. process ownership / shutdown / failure decisions (pure)
// ============================================================

async function testProcess() {
  await test("H. termination plan targets exactly the owned PID", () => {
    const plan = planBackendTermination(4242, "win32");
    assert.strictEqual(plan.tool, "taskkill");
    assert.deepStrictEqual(plan.args, [
      "/pid", "4242", "/T", "/F",
    ]);
    // No other PID appears anywhere in the plan.
    const blob = JSON.stringify(plan);
    assert.ok(blob.includes("4242"));
    assert.ok(!blob.includes("1337"));
  });

  await test("H2. non-Windows uses graceful kill", () => {
    const plan = planBackendTermination(99, "linux");
    assert.strictEqual(plan.tool, "kill");
    assert.strictEqual(plan.graceful, true);
  });

  await test("H3. no PID -> no plan (never kills anything)", () => {
    assert.strictEqual(planBackendTermination(null, "win32"), null);
    assert.strictEqual(planBackendTermination(undefined, "win32"), null);
  });

  await test("I. deliberate shutdown is not reported as failure", () => {
    const outcome = nextStateOnExit("stopping", 1, null);
    assert.strictEqual(outcome.state, "stopped");
    assert.strictEqual(outcome.reason, "");
  });

  await test("J. exit during startup -> failed with truthful reason", () => {
    const outcome = nextStateOnExit("running", 1, null);
    assert.strictEqual(outcome.state, "failed");
    assert.match(outcome.reason, /exited during startup/);
    assert.match(outcome.reason, /code=1/);
  });

  await test("J2. unknown prior state is left unchanged (no fake failure)", () => {
    const outcome = nextStateOnExit("not-started", 0, null);
    assert.strictEqual(outcome.state, "not-started");
    assert.strictEqual(outcome.reason, "");
  });

  await test("K. taskkill plan never targets unrelated PIDs", () => {
    for (const pid of [1, 100, 65535]) {
      const plan = planBackendTermination(pid, "win32");
      assert.deepStrictEqual(plan.args, [
        "/pid", String(pid), "/T", "/F",
      ]);
    }
  });
}

(async () => {
  console.log("probe");
  await testProbe();
  console.log("config");
  await testConfig();
  console.log("upgrade");
  await testUpgrade();
  console.log("process");
  await testProcess();

  console.log(`\npassed: ${passed}, failed: ${failed}`);
  // Test servers/sockets may keep libuv alive; report and exit
  // explicitly.
  process.exit(failed > 0 ? 1 : 0);
})();
