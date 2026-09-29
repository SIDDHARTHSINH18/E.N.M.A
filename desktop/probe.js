// ENMA desktop — backend port probe.
//
// Extracted verbatim from main.js (parameterized host/port) so
// the healthy/foreign/free classification is unit-testable
// without Electron. Semantics unchanged:
//   healthy — an HTTP server answers on /health (an ENMA
//             backend already owns the port; reuse it)
//   foreign — something accepts TCP but does not answer /health
//   free    — nothing accepts the connection

const http = require("http");
const net = require("net");

function probeBackend({ host, port, timeoutMs = 1500 }) {
  return new Promise((resolve) => {
    const socket = net.connect(port, host);
    socket.setTimeout(timeoutMs);

    const finish = (answer) => {
      socket.destroy();
      resolve(answer);
    };

    socket.on("connect", () => {
      // The pre-connect "timeout -> free" listener must not race
      // the post-connect "timeout -> foreign" one: once TCP is
      // connected, a silent port is FOREIGN (something owns it
      // and cannot bind a new backend), never "free". Without
      // removing the earlier listener, Node fires both and the
      // first-registered "free" wins the race.
      socket.removeAllListeners("timeout");

      http
        .get(
          {
            host,
            port,
            path: "/health",
            // One-shot probe: never leave keep-alive sockets
            // holding the Electron process open.
            agent: false,
          },
          (res) => {
            res.resume();
            // Any HTTP response means an ENMA backend already
            // owns the port (/health is public and unauthenticated
            // by design).
            finish("healthy");
          }
        )
        .on("error", () => finish("foreign"));

      socket.setTimeout(timeoutMs, () => finish("foreign"));
    });

    socket.on("error", () => finish("free"));
    socket.on("timeout", () => finish("free"));
  });
}

module.exports = { probeBackend };
