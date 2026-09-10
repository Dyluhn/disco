const { createClient } = require("@libsql/client");

const client = createClient({ url: process.env.DATABASE_URL });
const port = process.env.PORT || 3000;

require("http")
  .createServer(async (_req, res) => {
    const rows = await client.execute("select count(*) as n from app");
    res.end(JSON.stringify(rows.rows));
  })
  .listen(port);
