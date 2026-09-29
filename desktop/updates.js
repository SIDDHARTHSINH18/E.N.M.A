// ENMA desktop — update check logic.
//
// Smallest legitimate mechanism for the existing architecture:
// the shell fetches a version manifest over HTTPS from a
// configurable URL (default: the project's GitHub releases
// feed), compares it with the running version, and hands the
// TRUSTED download URL to the renderer. Download + install go
// through the user's default browser and the normal signed
// NSIS installer — ENMA never replaces its own executable and
// never bypasses Windows security. If Smart App Control or
// SmartScreen later blocks an unsigned installer, that is
// reported by Windows itself and the UI surfaces the honest
// check result instead of pretending an update was installed.

const https = require("https");

const DEFAULT_MANIFEST_URL =
  process.env.ENMA_UPDATE_MANIFEST_URL ||
  "https://api.github.com/repos/SIDDHARTHSINH18/E.N.M.A/releases/latest";

function parseVersion(version) {
  // "0.1.0" -> [0, 1, 0]; non-numeric parts are ignored.
  if (typeof version !== "string") {
    return null;
  }
  const core = version.trim().replace(/^v/i, "").split(".");
  if (!core.every((part) => /^\d+$/.test(part))) {
    return null;
  }
  return core.map(Number);
}

function isNewerVersion(latest, current) {
  const a = parseVersion(latest);
  const b = parseVersion(current);
  if (!a || !b) {
    return false;
  }
  const len = Math.max(a.length, b.length);
  for (let i = 0; i < len; i++) {
    const diff = (a[i] || 0) - (b[i] || 0);
    if (diff !== 0) {
      return diff > 0;
    }
  }
  return false;
}

/**
 * Normalize a release manifest into an update decision.
 * Supports the GitHub releases API shape (tag_name/html_url)
 * and a minimal custom shape ({version, url, notes}).
 */
function buildUpdateInfo({ currentVersion, manifest }) {
  if (!manifest || typeof manifest !== "object") {
    return {
      available: false,
      current: currentVersion,
      latest: null,
      url: null,
      error: "update manifest was empty or malformed",
    };
  }

  const latest =
    typeof manifest.version === "string"
      ? manifest.version
      : typeof manifest.tag_name === "string"
        ? manifest.tag_name
        : null;

  const url =
    typeof manifest.url === "string"
      ? manifest.url
      : typeof manifest.html_url === "string"
        ? manifest.html_url
        : null;

  if (!latest || !url) {
    return {
      available: false,
      current: currentVersion,
      latest: null,
      url: null,
      error: "update manifest is missing version or download URL",
    };
  }

  if (!/^https:\/\//i.test(url)) {
    // Updates are only ever offered over HTTPS.
    return {
      available: false,
      current: currentVersion,
      latest,
      url: null,
      error: "update URL is not HTTPS; refusing to offer it",
    };
  }

  return {
    available: isNewerVersion(latest, currentVersion),
    current: currentVersion,
    latest,
    url,
    error: null,
  };
}

function fetchManifest(manifestUrl, timeoutMs = 10000, _redirects = 0) {
  return new Promise((resolve, reject) => {
    https
      .get(
        manifestUrl,
        {
          headers: {
            "User-Agent": "ENMA-Desktop-Update-Check",
            Accept: "application/vnd.github+json",
          },
          timeout: timeoutMs,
        },
        (res) => {
          // Follow https->https redirects (repo renames produce
          // a 301); never downgrade scheme, cap the hops.
          if (
            res.statusCode &&
            res.statusCode >= 300 &&
            res.statusCode < 400 &&
            typeof res.headers.location === "string"
          ) {
            res.resume();
            if (_redirects >= 3) {
              reject(new Error("update manifest: too many redirects"));
              return;
            }
            const location = new URL(res.headers.location, manifestUrl);
            if (location.protocol !== "https:") {
              reject(new Error("update manifest redirect is not HTTPS"));
              return;
            }
            fetchManifest(location.toString(), timeoutMs, _redirects + 1)
              .then(resolve)
              .catch(reject);
            return;
          }

          if (res.statusCode !== 200) {
            res.resume();
            reject(
              new Error(`manifest fetch failed: HTTP ${res.statusCode}`)
            );
            return;
          }

          let body = "";
          res.setEncoding("utf-8");
          res.on("data", (chunk) => (body += chunk));
          res.on("end", () => {
            try {
              resolve(JSON.parse(body));
            } catch (error) {
              reject(new Error("update manifest was not valid JSON"));
            }
          });
        }
      )
      .on("timeout", () =>
        reject(new Error("update manifest fetch timed out"))
      )
      .on("error", reject);
  });
}

async function checkForUpdate({
  currentVersion,
  manifestUrl = DEFAULT_MANIFEST_URL,
  fetcher = fetchManifest,
} = {}) {
  try {
    const manifest = await fetcher(manifestUrl);
    return buildUpdateInfo({ currentVersion, manifest });
  } catch (error) {
    return {
      available: false,
      current: currentVersion,
      latest: null,
      url: null,
      error: `${typeOf(error)}: ${error.message}`,
    };
  }
}

function typeOf(error) {
  return (error && error.constructor && error.constructor.name) || "Error";
}

module.exports = {
  DEFAULT_MANIFEST_URL,
  parseVersion,
  isNewerVersion,
  buildUpdateInfo,
  fetchManifest,
  checkForUpdate,
};
