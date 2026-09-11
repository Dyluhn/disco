# Photo and video uploads

> Multipart uploads to a durable volume with validation, per-listing ordering, previews, reordering/removal, a gallery/carousel and an embedded video player with range requests.

## Env / limits
```
UPLOAD_DIR          # default ./data/uploads (on the compose volume)
UPLOAD_MAX_MB       # default 200 (video), images are capped at 15 MB in code
```
`npm i busboy@^1.6.0` in `api/`. Accept images `image/jpeg|png|webp|gif` and video `video/mp4|webm|quicktime`; sniff the magic bytes (first 12 bytes) — do not trust the client's `Content-Type` or extension.

## Schema
```sql
CREATE TABLE media(id INTEGER PRIMARY KEY, owner_id INTEGER NOT NULL, listing_id INTEGER NOT NULL,
  kind TEXT NOT NULL CHECK(kind IN ('photo','video')), mime TEXT NOT NULL, bytes INTEGER NOT NULL,
  storage_path TEXT NOT NULL, position INTEGER NOT NULL, width INTEGER, height INTEGER, created_at TEXT NOT NULL);
CREATE INDEX media_listing_pos ON media(listing_id, position);
```

## Upload handler (streams to disk, never buffers a video in memory)
```js
import Busboy from "busboy"; import { createWriteStream } from "node:fs"; import { mkdir, stat, unlink } from "node:fs/promises";
import { randomUUID } from "node:crypto"; import path from "node:path";
export function readUploads(req, { maxBytes, accept }) {          // resolves [{ field, filename, mime, tmpPath, bytes }]
  return new Promise((resolve, reject) => {
    const files = []; const bb = Busboy({ headers: req.headers, limits: { fileSize: maxBytes, files: 20 } });
    bb.on("file", (field, stream, info) => {
      const tmpPath = path.join(process.env.UPLOAD_DIR ?? "./data/uploads", "tmp", randomUUID());
      let head = Buffer.alloc(0), bytes = 0;
      mkdir(path.dirname(tmpPath), { recursive: true }).then(() => {
        const out = createWriteStream(tmpPath);
        stream.on("data", c => { bytes += c.length; if (head.length < 16) head = Buffer.concat([head, c]).subarray(0, 16); });
        stream.on("limit", () => { out.destroy(); unlink(tmpPath).catch(() => {}); reject(new Error("file too large")); });
        stream.pipe(out).on("finish", () => { const mime = sniff(head);
          if (!mime || !accept.test(mime)) { unlink(tmpPath).catch(() => {}); return reject(new Error("unsupported file type")); }
          files.push({ field, filename: info.filename, mime, tmpPath, bytes }); });
      });
    });
    bb.on("close", () => resolve(files)); bb.on("error", reject); req.pipe(bb);
  });
}
export function sniff(b) {
  if (b[0] === 0xff && b[1] === 0xd8) return "image/jpeg";
  if (b.subarray(0, 8).equals(Buffer.from([0x89,0x50,0x4e,0x47,0x0d,0x0a,0x1a,0x0a]))) return "image/png";
  if (b.subarray(0, 4).toString() === "RIFF" && b.subarray(8, 12).toString() === "WEBP") return "image/webp";
  if (b.subarray(0, 3).toString() === "GIF") return "image/gif";
  if (b.subarray(4, 8).toString() === "ftyp") return "video/mp4";                 // mp4 / mov family
  if (b.subarray(0, 4).equals(Buffer.from([0x1a,0x45,0xdf,0xa3]))) return "video/webm";
  return null;
}
```
`POST /api/listings/:id/media` (owner only): move each tmp file to `<UPLOAD_DIR>/<listingId>/<uuid>.<ext>`, insert with `position = max(position)+1`, respond with the rows. `PATCH /api/listings/:id/media/order` `{ ids: [...] }` rewrites `position` in one transaction. `DELETE /api/media/:id` removes the row then the file.

## Serving
`GET /media/:id` → look up the row (public listings: anyone; drafts: owner), set `Content-Type` from the row, `Cache-Control: public, max-age=31536000, immutable`, and honour `Range` for video (`206 Partial Content`, `Content-Range`, `Accept-Ranges: bytes`) — `<video>` seeking depends on it. Never build the path from user input; use the stored `storage_path` under `UPLOAD_DIR` only.

## Client
- Uploader: `<input type=file multiple accept="image/*,video/*">` + drop zone; show local previews (`URL.createObjectURL`) with a progress bar per file (`XMLHttpRequest` for `upload.onprogress`); a Remove control per item; drag-to-reorder (keyboard-accessible up/down buttons as the fallback) posting the new order.
- Detail page: photo carousel (current index, arrows, thumbnails, `loading="lazy"`), then the video(s) in `<video controls preload="metadata" src="/media/123">`. First photo is the listing cover.

## Prove it
Upload 3 photos + 1 mp4 to a listing as the host; they appear in order, reorder persists after reload, delete removes the file from disk; the detail page carousel cycles and the video plays and seeks (network tab shows `206`); a non-owner gets 403 on upload/delete; a renamed `.exe` as `.jpg` is rejected; files survive `compose down && up`.
