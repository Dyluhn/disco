// A tiny server that reads its secret from the environment at runtime — the
// value is NEVER hardcoded here. The planted sentinel lives only in `.env`
// (which must never ship in a release).
const http = require("http");

const apiKey = process.env.OPENAI_API_KEY;
const port = process.env.PORT || 8080;

http
  .createServer((req, res) => {
    if (req.url === "/healthz") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    res.writeHead(200);
    res.end("hello");
  })
  .listen(port, () => {
    console.log("listening on", port, "key set:", Boolean(apiKey));
  });
