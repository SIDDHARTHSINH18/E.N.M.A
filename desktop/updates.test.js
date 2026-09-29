// Tests for desktop/updates.js — the in-app update check logic.
// Run: node config.test.js (includes these via require? no —)
// Run: node updates.test.js
// No network: fetchers are faked; manifests are fixture objects.

const assert = require("assert");
const {
  parseVersion,
  isNewerVersion,
  buildUpdateInfo,
  checkForUpdate,
} = require("./updates");

let passed = 0;
let failed = 0;

async function test(name, fn) {
  try {
    await fn();
    passed++;
    console.log(`  ok - ${name}`);
  } catch (error) {
    failed++;
    console.error(`  FAIL - ${name}\n    ${error.message}`);
  }
}

(async () => {
  console.log("updates");

  await test("parseVersion handles plain and v-prefixed versions", () => {
    assert.deepStrictEqual(parseVersion("0.1.0"), [0, 1, 0]);
    assert.deepStrictEqual(parseVersion("v1.2.3"), [1, 2, 3]);
    assert.strictEqual(parseVersion("abc"), null);
    assert.strictEqual(parseVersion(null), null);
  });

  await test("isNewerVersion is semver-aware (no string compare)", () => {
    assert.strictEqual(isNewerVersion("0.2.0", "0.1.0"), true);
    assert.strictEqual(isNewerVersion("0.10.0", "0.9.0"), true);
    assert.strictEqual(isNewerVersion("0.1.0", "0.1.0"), false);
    assert.strictEqual(isNewerVersion("0.1.0", "0.2.0"), false);
    assert.strictEqual(isNewerVersion("1.0.0", "0.99.99"), true);
  });

  await test("GitHub release manifest shape is understood", () => {
    const info = buildUpdateInfo({
      currentVersion: "0.1.0",
      manifest: {
        tag_name: "v0.2.0",
        html_url: "https://github.com/x/releases/tag/v0.2.0",
      },
    });
    assert.strictEqual(info.available, true);
    assert.strictEqual(info.latest, "v0.2.0");
    assert.ok(info.url.startsWith("https://"));
  });

  await test("same version -> no update offered", () => {
    const info = buildUpdateInfo({
      currentVersion: "0.2.0",
      manifest: {
        tag_name: "v0.2.0",
        html_url: "https://github.com/x/releases/tag/v0.2.0",
      },
    });
    assert.strictEqual(info.available, false);
    assert.strictEqual(info.error, null);
  });

  await test("non-HTTPS update URL is refused", () => {
    const info = buildUpdateInfo({
      currentVersion: "0.1.0",
      manifest: { version: "9.9.9", url: "http://insecure.example/x.exe" },
    });
    assert.strictEqual(info.available, false);
    assert.match(info.error, /HTTPS/);
  });

  await test("malformed manifest is reported honestly", () => {
    const info = buildUpdateInfo({ currentVersion: "0.1.0", manifest: null });
    assert.strictEqual(info.available, false);
    assert.ok(info.error);
  });

  await test("checkForUpdate reports fetch failures truthfully", async () => {
    const info = await checkForUpdate({
      currentVersion: "0.1.0",
      fetcher: async () => {
        throw new Error("HTTP 503");
      },
    });
    assert.strictEqual(info.available, false);
    assert.match(info.error, /HTTP 503/);
  });

  await test("checkForUpdate end-to-end with a fake fetcher", async () => {
    const info = await checkForUpdate({
      currentVersion: "0.1.0",
      fetcher: async () => ({
        tag_name: "v0.3.0",
        html_url: "https://github.com/x/releases/tag/v0.3.0",
      }),
    });
    assert.strictEqual(info.available, true);
    assert.strictEqual(info.latest, "v0.3.0");
  });

  console.log(`\npassed: ${passed}, failed: ${failed}`);
  process.exit(failed > 0 ? 1 : 0);
})();
