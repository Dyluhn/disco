// Free-form Build fixture: a minimal Express web app for the self-host E2E.
// Binds the $PORT contract; GET / returns a meaningful HTML body so the
// generated compose healthcheck (which hits "/") can observe a real 200.
const express = require("express");

const app = express();
const PORT = process.env.PORT || 8080;

app.get("/", (_req, res) => {
  res
    .type("html")
    .send(
      "<!doctype html><meta charset=utf-8><title>build-express</title>" +
        "<h1>build-express</h1><p>Disco self-host E2E fixture is running.</p>"
    );
});

app.get("/api/echo", express.json(), (req, res) => {
  res.json({ service: "build-express", ok: true, body: req.body ?? null });
});

app.listen(PORT, () => {
  // eslint-disable-next-line no-console
  console.log(`build-express listening on ${PORT}`);
});
