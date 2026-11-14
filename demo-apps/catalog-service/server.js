// catalog-service: a stand-in for "some other team's app".
// Plain Node.js, no dependencies, no monitoring SDK. It only writes ordinary
// text logs to stdout. The monitoring platform reads those logs from Docker;
// nothing here knows it exists.
//
//   GET  /products        the business endpoint (uses a simulated database)
//   GET  /health
//   POST /chaos?mode=...  ok | db_down | timeout | payment_errors   (demo only)
const http = require("http");
const crypto = require("crypto");

const PORT = Number(process.env.PORT || 8090);
const DB_HOST = process.env.DB_HOST || "10.5.0.12:5432";
let mode = process.env.CHAOS_MODE || "ok";

function log(level, msg) {
  process.stdout.write(`${new Date().toISOString()} ${level.padEnd(5)} [catalog-service] ${msg}\n`);
}

const PRODUCTS = [
  { sku: "SKU-001", name: "Widget", price: 19.99 },
  { sku: "SKU-002", name: "Gadget", price: 34.5 },
];

function queryProducts() {
  return new Promise((resolve, reject) => {
    if (mode === "db_down") {
      return setTimeout(() => reject(Object.assign(
        new Error(`SequelizeConnectionRefusedError: connect ECONNREFUSED ${DB_HOST}`), { code: "ECONNREFUSED" })), 15);
    }
    if (mode === "timeout") {
      return setTimeout(() => reject(Object.assign(
        new Error(`TimeoutError: query to products table timed out after 3000ms (pool ${DB_HOST})`), { code: "ETIMEDOUT" })), 300);
    }
    setTimeout(() => resolve(PRODUCTS), 5 + Math.random() * 10);
  });
}

function send(res, status, body) {
  res.writeHead(status, { "Content-Type": "application/json" });
  res.end(JSON.stringify(body));
}

const server = http.createServer(async (req, res) => {
  const started = Date.now();
  const reqId = crypto.randomBytes(6).toString("hex");
  const url = new URL(req.url, `http://${req.headers.host}`);

  if (req.method === "GET" && url.pathname === "/health") return send(res, 200, { status: "ok", mode });

  if (req.method === "POST" && url.pathname === "/chaos") {
    const next = url.searchParams.get("mode") || "ok";
    if (!["ok", "db_down", "timeout", "payment_errors"].includes(next)) return send(res, 400, { error: "unknown mode" });
    mode = next;
    log("INFO", `chaos mode set to ${mode}`);
    return send(res, 200, { mode });
  }

  if (req.method === "GET" && url.pathname === "/products") {
    try {
      const products = await queryProducts();
      if (mode === "payment_errors" && Math.random() < 0.7) {
        log("ERROR", `Payment gateway rejected price-check request: card network unavailable (req=${reqId})`);
        return send(res, 502, { error: "pricing unavailable" });
      }
      log("INFO", `GET /products 200 ${Date.now() - started}ms req=${reqId}`);
      return send(res, 200, products);
    } catch (err) {
      log("ERROR", `${err.message} while handling GET /products (req=${reqId})`);
      log("WARN", `GET /products status=500 ${Date.now() - started}ms req=${reqId}`);
      return send(res, 500, { error: "internal error" });
    }
  }
  send(res, 404, { error: "not found" });
});

server.listen(PORT, () => log("INFO", `listening on :${PORT} (chaos mode: ${mode})`));
