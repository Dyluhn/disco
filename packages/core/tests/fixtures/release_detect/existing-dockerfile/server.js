const http = require("http");

const port = process.env.PORT || 8080;

http
  .createServer((_req, res) => {
    res.end("owner-built server");
  })
  .listen(port);
