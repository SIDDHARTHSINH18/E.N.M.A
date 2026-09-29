// ENMA desktop — backend configuration bootstrap + upgrade.
//
// Extracted from main.js so the logic is unit-testable without
// Electron. The desktop shell (main.js) calls into this module;
// behavior is unchanged apart from the documented upgrade path:
// an EXISTING config.env that predates ENMA_DEFAULT_PROVIDER is
// reconciled (idempotently, preserving every existing byte of
// user configuration) instead of being left stale.

const path = require("path");

// The provider the app actually talks to (chat hardcodes Groq;
// the planner defaults to Groq). Startup validation in
// backend/main.py checks THIS provider's key, so the template
// and the upgrade path must agree with it.
const DEFAULT_PROVIDER = "groq";

function buildConfigContent(passphrase) {
  return [
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
    "# The app talks to the Groq provider; startup validation",
    "# checks this provider's key:",
    "ENMA_DEFAULT_PROVIDER=groq",
    "#",
    "# Provider key for the provider above (required for chat;",
    "# startup refuses to run without it):",
    "# GROQ_API_KEY=gsk_...",
    "",
    "# Other provider keys are optional; without them those",
    "# providers report themselves unreachable:",
    "GEMINI_API_KEY=",
    "",
  ].join("\n");
}

function hasNonEmptyValue(content, key) {
  const re = new RegExp(`^${key}=.+$`, "m");
  return re.test(content);
}

function isExplicitlyConfigured(content, key) {
  // A non-commented line for the key exists (even if empty).
  const re = new RegExp(`^${key}=`, "m");
  return re.test(content);
}

function chooseDefaultProvider(content) {
  // Policy derived from the existing architecture:
  // - the app's chat/planner provider is Groq, so Groq wins
  //   whenever it is a plausible choice;
  // - a user who explicitly configured ONLY a Gemini key is
  //   left on Gemini — never break their working setup;
  // - both keys -> Groq (matches chat behavior);
  // - no keys at all -> Groq, so the startup error message
  //   points at the key the template documents.
  if (
    !hasNonEmptyValue(content, "GROQ_API_KEY") &&
    hasNonEmptyValue(content, "GEMINI_API_KEY")
  ) {
    return "gemini";
  }
  return DEFAULT_PROVIDER;
}

function upgradeBackendConfigContent(content) {
  // Returns { content, changed, reason }. Idempotent: an
  // explicit ENMA_DEFAULT_PROVIDER line always wins and nothing
  // is rewritten. Existing credentials are never touched.
  if (isExplicitlyConfigured(content, "ENMA_DEFAULT_PROVIDER")) {
    return { content, changed: false, reason: "already-configured" };
  }

  const provider = chooseDefaultProvider(content);

  const line = `ENMA_DEFAULT_PROVIDER=${provider}`;

  // Insert before the first provider-key line so the file stays
  // readable; otherwise append.
  const lines = content.split("\n");
  const insertAt = lines.findIndex((l) =>
    /^\s*#?\s*(GROQ|GEMINI|NVIDIA|HF|HUGGINGFACE)_API_KEY=/.test(l)
  );

  if (insertAt === -1) {
    const next = content.endsWith("\n")
      ? `${content}${line}\n`
      : `${content}\n${line}`;
    return { content: next, changed: true, reason: "appended" };
  }

  lines.splice(insertAt, 0, line);
  return {
    content: lines.join("\n"),
    changed: true,
    reason: `inserted:${provider}`,
  };
}

function ensureBackendConfig({ configPath, fs, crypto }) {
  if (!fs.existsSync(configPath)) {
    const passphrase = crypto.randomBytes(24).toString("base64url");

    fs.writeFileSync(
      configPath,
      buildConfigContent(passphrase),
      { encoding: "utf-8" }
    );

    console.log(
      `ENMA first-run configuration created at ${configPath}.`
    );

    return configPath;
  }

  // Existing configuration: reconcile for the upgrade path.
  // Never overwrite credentials; only add ENMA_DEFAULT_PROVIDER
  // when genuinely missing and compatible.
  const current = fs.readFileSync(configPath, "utf-8");
  const { content, changed, reason } = upgradeBackendConfigContent(
    current
  );

  if (changed) {
    fs.writeFileSync(configPath, content, { encoding: "utf-8" });
    console.log(
      `ENMA configuration updated (${reason}); ` +
      "existing values preserved."
    );
  }

  return configPath;
}

module.exports = {
  DEFAULT_PROVIDER,
  buildConfigContent,
  upgradeBackendConfigContent,
  chooseDefaultProvider,
  ensureBackendConfig,
};
