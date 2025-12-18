export default {
  async fetch(request, env) {
    if (request.method !== "PUT") {
      return new Response("Method Not Allowed", { status: 405 });
    }

    const auth = request.headers.get("x-upload-secret");
    if (!auth || auth !== env.UPLOAD_SECRET) {
      return new Response("Unauthorized", { status: 401 });
    }

    const url = new URL(request.url);
    const key = url.pathname.replace(/^\/+/, "");

    if (!key) {
      return new Response("Missing object key", { status: 400 });
    }

    try {
      await env.MEDIA_BUCKET.put(key, request.body, {
        httpMetadata: {
          contentType:
            request.headers.get("content-type") ||
            "application/octet-stream",
        },
      });

      return new Response(
        JSON.stringify({ success: true, key }),
        { status: 200, headers: { "Content-Type": "application/json" } }
      );
    } catch (err) {
      return new Response(
        JSON.stringify({ error: err.toString() }),
        { status: 500, headers: { "Content-Type": "application/json" } }
      );
    }
  },
};
