import type { Config } from "drizzle-kit";

export default {
  schema: "./src/schema.ts",
  out: "./drizzle",
  dialect: "sqlite",
  driver: "libsql",
  dbCredentials: {
    url: "file:./data/app.db",
  },
} satisfies Config;
