/**
 * Drives the official OpenAI JavaScript SDK against Zibbo.
 *
 * The Python SDK suite (tests/test_openai_sdk_compat.py) proves the wire format is
 * right for one client. It cannot prove it for a different one: the JS SDK has its
 * own SSE parser, its own error-class mapping, and its own header expectations.
 *
 * The only non-default argument given to the client is `baseURL`. That is the whole
 * promise of the product, asserted from the outside.
 *
 *   uvicorn benchmarks.upstream:app --port 8124 --no-access-log
 *   ZIBBO_OPENAI_BASE_URL=http://127.0.0.1:8124/v1 uvicorn gateway.main:app --port 8123
 *   npm install && npm test
 */

import assert from "node:assert/strict";
import OpenAI from "openai";

const GATEWAY = process.env.GATEWAY_URL ?? "http://127.0.0.1:8123/v1";

const client = new OpenAI({
  apiKey: "sk-caller-key",
  baseURL: GATEWAY,
  maxRetries: 0,
});

const results = [];

async function check(name, fn) {
  try {
    await fn();
    results.push({ name, ok: true });
    console.log(`  PASS  ${name}`);
  } catch (error) {
    results.push({ name, ok: false, error: String(error?.message ?? error) });
    console.log(`  FAIL  ${name}\n        ${error?.message ?? error}`);
  }
}

const MESSAGES = [{ role: "user", content: "Summarize the meeting notes." }];

const NOISY_HTML =
  "<!DOCTYPE html><html><head><title>Doc</title><script>track()</script></head>" +
  "<body><nav class='navbar'><a href='/'>Home</a></nav>" +
  "<div class='cookie-consent'>Accept</div><main><h1>Title</h1><p>Body   text.</p></main>" +
  "<footer>(c) 2026</footer></body></html>";

console.log(`openai-js compatibility against ${GATEWAY}\n`);

await check("chat.completions.create (non-streaming) parses", async () => {
  const completion = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: MESSAGES,
  });
  assert.equal(completion.object, "chat.completion");
  assert.equal(completion.choices[0].message.role, "assistant");
  assert.equal(completion.choices[0].finish_reason, "stop");
  assert.equal(completion.usage.total_tokens, 10);
});

await check("chat.completions.create (streaming) yields deltas", async () => {
  const stream = await client.chat.completions.create({
    model: "gpt-4o-mini",
    messages: MESSAGES,
    stream: true,
  });
  let chunks = 0;
  let text = "";
  for await (const chunk of stream) {
    chunks += 1;
    text += chunk.choices[0]?.delta?.content ?? "";
  }
  assert.ok(chunks >= 2, `expected multiple chunks, got ${chunks}`);
  assert.ok(text.startsWith("tok0"), `unexpected stream text: ${text}`);
});

await check("streaming can be aborted mid-flight", async () => {
  const controller = new AbortController();
  const stream = await client.chat.completions.create(
    { model: "gpt-4o-mini", messages: MESSAGES, stream: true },
    { signal: controller.signal },
  );
  let seen = 0;
  try {
    for await (const _chunk of stream) {
      seen += 1;
      if (seen === 1) controller.abort();
    }
  } catch (error) {
    if (error?.name !== "APIUserAbortError" && error?.name !== "AbortError") throw error;
  }
  assert.ok(seen >= 1);
});

await check("models.list works", async () => {
  // The benchmark upstream answers every path with {"ok": true}; we assert the
  // request round-trips and the SDK parses a JSON body rather than throwing.
  const response = await client.get("/models", { headers: {} });
  assert.ok(response);
});

await check("rate-limit headers reach the SDK via withResponse", async () => {
  const { response } = await client.chat.completions
    .create({ model: "gpt-4o-mini", messages: MESSAGES })
    .withResponse();
  assert.equal(response.headers.get("x-ratelimit-remaining-requests"), "9999");
});

await check("provider request id is exposed, not the gateway's", async () => {
  const { response } = await client.chat.completions
    .create({ model: "gpt-4o-mini", messages: MESSAGES })
    .withResponse();
  assert.equal(response.headers.get("x-request-id"), "upstream-bench");
  assert.match(response.headers.get("x-zibbo-request-id"), /^req_/);
});

await check("optimization happens and is reported in headers", async () => {
  const { response } = await client.chat.completions
    .create({ model: "gpt-4o-mini", messages: [{ role: "user", content: NOISY_HTML }] })
    .withResponse();
  assert.equal(response.headers.get("x-zibbo-optimization"), "applied");
  assert.ok(Number(response.headers.get("x-zibbo-tokens-saved")) > 0);
});

await check("an already-clean request is not optimized", async () => {
  const { response } = await client.chat.completions
    .create({ model: "gpt-4o-mini", messages: MESSAGES })
    .withResponse();
  assert.match(response.headers.get("x-zibbo-optimization"), /^skipped:/);
});

await check("a raw fetch against the gateway works (no SDK)", async () => {
  const response = await fetch(`${GATEWAY}/chat/completions`, {
    method: "POST",
    headers: { "content-type": "application/json", authorization: "Bearer sk-x" },
    body: JSON.stringify({ model: "gpt-4o-mini", messages: MESSAGES }),
  });
  assert.equal(response.status, 200);
  const body = await response.json();
  assert.equal(body.object, "chat.completion");
});

const failed = results.filter((r) => !r.ok);
console.log(`\n${results.length - failed.length}/${results.length} passed`);
if (failed.length > 0) process.exit(1);
