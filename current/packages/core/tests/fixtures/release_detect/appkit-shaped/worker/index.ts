export interface Env {
  DB: D1Database;
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const rows = await env.DB.prepare("select * from todo").all();
    return Response.json(rows.results);
  },
};
