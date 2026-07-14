import { readdirSync, statSync } from "node:fs";
import { resolve } from "node:path";

// Match Vite's default production JavaScript chunk advisory and make it a hard,
// reviewable build contract so feature growth cannot silently recreate B-003.
const MAX_CHUNK_BYTES = 500_000;
const assets = resolve(import.meta.dirname, "../dist/assets");
const oversized = readdirSync(assets)
  .filter((name) => name.endsWith(".js"))
  .map((name) => ({ name, bytes: statSync(resolve(assets, name)).size }))
  .filter(({ bytes }) => bytes > MAX_CHUNK_BYTES)
  .sort((left, right) => right.bytes - left.bytes);

if (oversized.length > 0) {
  const details = oversized.map(({ name, bytes }) => `${name}: ${bytes} bytes`).join("\n");
  throw new Error(
    `Production JavaScript chunk budget exceeded (${MAX_CHUNK_BYTES} bytes):\n${details}`,
  );
}

const chunks = readdirSync(assets).filter((name) => name.endsWith(".js"));
const largest = chunks
  .map((name) => ({ name, bytes: statSync(resolve(assets, name)).size }))
  .sort((left, right) => right.bytes - left.bytes)[0];
console.log(
  `Bundle budget passed: ${chunks.length} JavaScript chunks; largest ${largest.name} ` +
    `is ${largest.bytes} bytes (limit ${MAX_CHUNK_BYTES}).`,
);
