// Imported fixture: a plain Node http server (no dependencies) shipped with a
// package-lock.json, so the generated Dockerfile takes the `npm ci` path. Binds
// the $PORT contract; GET / returns a meaningful HTML body for the healthcheck.
const http = require("http");

const PORT = process.env.PORT || 8080;

const server = http.createServer((req, res) => {
  if (req.url === "/api/health") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ service: "imported-node", ok: true }));
    return;
  }
  res.writeHead(200, { "Content-Type": "text/html" });
  res.end(
    "<!doctype html><meta charset=utf-8><title>imported-node</title>" +
      "<h1>imported-node</h1><p>Disco self-host E2E fixture is running.</p>"
  );
});

server.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`imported-node listening on ${PORT}`);
});
