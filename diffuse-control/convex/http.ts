import { FunctionArgs, httpRouter } from "convex/server";

import { internal } from "./_generated/api";
import { httpAction } from "./_generated/server";

const http = httpRouter();
const MAX_BODY_BYTES = 256 * 1024;
const MAX_CLOCK_SKEW_SECONDS = 300;

function hex(bytes: ArrayBuffer): string {
  return Array.from(new Uint8Array(bytes))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function equal(left: string, right: string): boolean {
  if (left.length !== right.length) return false;
  let difference = 0;
  for (let index = 0; index < left.length; index += 1) {
    difference |= left.charCodeAt(index) ^ right.charCodeAt(index);
  }
  return difference === 0;
}

http.route({
  path: "/v1/data-plane/snapshot",
  method: "POST",
  handler: httpAction(async (ctx, request) => {
    const secret = process.env.DIFFUSE_CONTROL_PLANE_SIGNING_KEY;
    if (!secret) {
      return new Response("Control-plane ingest is not configured", {
        status: 503,
      });
    }
    const timestampText = request.headers.get("x-diffuse-timestamp");
    const signature = request.headers.get("x-diffuse-signature");
    const timestamp = Number(timestampText);
    if (
      !timestampText ||
      !signature ||
      !Number.isSafeInteger(timestamp) ||
      Math.abs(Math.floor(Date.now() / 1000) - timestamp) >
        MAX_CLOCK_SKEW_SECONDS
    ) {
      return new Response("Invalid signature metadata", { status: 401 });
    }
    const body = await request.text();
    if (new TextEncoder().encode(body).byteLength > MAX_BODY_BYTES) {
      return new Response("Snapshot is too large", { status: 413 });
    }
    const key = await crypto.subtle.importKey(
      "raw",
      new TextEncoder().encode(secret),
      { name: "HMAC", hash: "SHA-256" },
      false,
      ["sign"],
    );
    const digest = await crypto.subtle.sign(
      "HMAC",
      key,
      new TextEncoder().encode(`${timestampText}.${body}`),
    );
    if (!equal(signature, `sha256=${hex(digest)}`)) {
      return new Response("Invalid signature", { status: 401 });
    }

    let payload: unknown;
    try {
      payload = JSON.parse(body);
    } catch {
      return new Response("Invalid JSON", { status: 400 });
    }
    if (
      typeof payload !== "object" ||
      payload === null ||
      !("deployment" in payload) ||
      typeof payload.deployment !== "object" ||
      payload.deployment === null ||
      !("repositories" in payload) ||
      !Array.isArray(payload.repositories) ||
      payload.repositories.length > 50 ||
      !("reviews" in payload) ||
      !Array.isArray(payload.reviews) ||
      payload.reviews.length > 40
    ) {
      return new Response("Invalid or unbounded snapshot", { status: 400 });
    }
    try {
      await ctx.runMutation(
        internal.controlPlane.ingestSnapshot,
        payload as FunctionArgs<typeof internal.controlPlane.ingestSnapshot>,
      );
    } catch {
      return new Response("Snapshot validation failed", { status: 400 });
    }
    return Response.json({ accepted: true }, { status: 202 });
  }),
});

export default http;
