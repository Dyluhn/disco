const http = require("http");

const port = process.env.PORT;

http
  .createServer((req, res) => {
    if (req.url === "/healthz") {
      res.writeHead(200);
      res.end("ok");
      return;
    }
    res.writeHead(200);
    res.end("owner-declared release target");
  })
  .listen(port, "0.0.0.0");
