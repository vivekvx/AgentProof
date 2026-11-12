import assert from "node:assert/strict";
import http from "node:http";
import { once } from "node:events";
import { test } from "node:test";
import { createServer } from "vite";

test("Vite API proxy adds the local bearer token without exposing it to the browser", async () => {
  const token = "ephemeral-proxy-test-token-0123456789abcdef";
  let receivedAuthorization;
  const upstream = http.createServer((request, response) => {
    receivedAuthorization = request.headers.authorization;
    response.writeHead(204).end();
  });
  upstream.listen(0, "127.0.0.1");
  await once(upstream, "listening");

  const priorToken = process.env.AGENTPROOF_API_TOKEN;
  const priorTarget = process.env.API_PROXY_TARGET;
  process.env.AGENTPROOF_API_TOKEN = token;
  process.env.API_PROXY_TARGET = `http://127.0.0.1:${upstream.address().port}`;
  let vite;
  try {
    vite = await createServer({
      configFile: new URL("../vite.config.ts", import.meta.url).pathname,
      server: { host: "127.0.0.1", port: 0 },
    });
    await vite.listen();
    const address = vite.httpServer.address();
    const response = await fetch(`http://127.0.0.1:${address.port}/api/v2/datasets`);
    assert.equal(response.status, 204);
    assert.equal(receivedAuthorization, `Bearer ${token}`);
    assert.equal(response.headers.get("authorization"), null);

    const forged = await fetch(`http://127.0.0.1:${address.port}/api/v2/datasets`, {
      headers: { Authorization: "Bearer browser-supplied-token" },
    });
    assert.equal(forged.status, 204);
    assert.equal(receivedAuthorization, `Bearer ${token}`);
    assert.equal(forged.headers.get("authorization"), null);
  } finally {
    await vite?.close();
    await new Promise((resolve) => upstream.close(resolve));
    if (priorToken === undefined) delete process.env.AGENTPROOF_API_TOKEN;
    else process.env.AGENTPROOF_API_TOKEN = priorToken;
    if (priorTarget === undefined) delete process.env.API_PROXY_TARGET;
    else process.env.API_PROXY_TARGET = priorTarget;
  }
});
