// "Shop Demo" — a small online-shop API used to demo Liara Copilot.
// It contains REAL, intentional bugs so the SRE agent has something to find:
//   1) /api/products needs DATABASE_URL — if it's missing every request returns 500
//   2) cacheResult() keeps every search result forever -> memory leak -> slowness / OOM
//   3) /api/report does heavy synchronous work -> CPU saturation
const http = require('http');
const { getDb } = require('./src/db');

const PORT = process.env.PORT || 3000;
const searchCache = []; // BUG: unbounded, never evicted

function cacheResult(query, result) {
  // BUG (intentional): stores a large copy of every result, forever
  searchCache.push({ query, at: Date.now(), payload: JSON.stringify(result).repeat(200) });
}

function send(res, code, body) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(body));
}

const routes = {
  '/': (req, res) => {
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(`<html dir="rtl"><body style="font-family:Tahoma;padding:40px">
      <h1>🛒 فروشگاه دمو</h1><p>این سایت روی لیارا اجرا می‌شود.</p>
      <ul><li><a href="/api/products">/api/products</a></li><li><a href="/api/search?q=phone">/api/search</a></li>
      <li><a href="/api/report">/api/report</a></li><li><a href="/health">/health</a></li></ul></body></html>`);
  },
  '/health': (req, res) => send(res, 200, { ok: true, uptime: process.uptime() }),
  '/api/products': (req, res) => {
    const db = getDb(); // throws if DATABASE_URL is missing
    send(res, 200, db.products());
  },
  '/api/search': (req, res, url) => {
    const q = (url.searchParams.get('q') || '').toLowerCase();
    const db = getDb();
    const result = db.products().filter((p) => p.name.toLowerCase().includes(q));
    cacheResult(q, result);
    send(res, 200, { q, result, cached: searchCache.length });
  },
  // Demo helper: simulates heavy traffic on /api/search so the leak shows up quickly.
  '/api/simulate-traffic': (req, res, url) => {
    const n = Math.min(parseInt(url.searchParams.get('n') || '2000', 10), 20000);
    const db = getDb();
    for (let i = 0; i < n; i++) cacheResult('q' + i, db.products());
    const mb = Math.round(process.memoryUsage().heapUsed / 1e6);
    console.log(`simulated ${n} searches, heap=${mb}MB, cache=${searchCache.length}`);
    send(res, 200, { simulated: n, heapMB: mb });
  },
  '/api/report': (req, res) => {
    // BUG (intentional): CPU-bound work on the request thread
    let x = 0;
    for (let i = 0; i < 6e8; i++) x += Math.sqrt(i);
    send(res, 200, { report: 'ok', x });
  },
};

const server = http.createServer((req, res) => {
  const started = Date.now();
  const url = new URL(req.url, 'http://localhost');
  res.on('finish', () => {
    console.log(`${req.method} ${url.pathname} ${res.statusCode} ${Date.now() - started}ms`);
  });
  const handler = routes[url.pathname];
  if (!handler) return send(res, 404, { error: 'not found' });
  try {
    handler(req, res, url);
  } catch (err) {
    console.error(`Error: ${err.message}`);
    console.error(err.stack.split('\n').slice(1, 3).join('\n'));
    send(res, 500, { error: 'Internal Server Error' });
  }
});

setInterval(() => {
  const mb = Math.round(process.memoryUsage().heapUsed / 1e6);
  if (mb > 150) console.warn(`warning: high memory usage heap=${mb}MB cache=${searchCache.length}`);
}, 15000);

server.listen(PORT, () => console.log(`Server listening on port ${PORT}`));
