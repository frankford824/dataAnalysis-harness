// 台账助手接到 Pi 上：工具从助手服务拉取、执行也回到助手服务（确认卡片、留存、费用都在那边）。
import http from "node:http";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const BASE = new URL(process.env.LEDGER_PI_URL ?? "http://127.0.0.1:8020");
const TOKEN = process.env.LEDGER_PI_TOKEN ?? "";

function request(method: string, path: string, body?: unknown, signal?: AbortSignal): Promise<any> {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? undefined : Buffer.from(JSON.stringify(body), "utf-8");
    // 不用 fetch：它默认 5 分钟没响应头就断，自动结账这类工具会跑得更久。
    const req = http.request(
      {
        hostname: BASE.hostname,
        port: BASE.port,
        path,
        method,
        headers: {
          Authorization: `Bearer ${TOKEN}`,
          "Content-Type": "application/json",
          ...(data ? { "Content-Length": data.length } : {}),
        },
      },
      (res) => {
        const chunks: Buffer[] = [];
        res.on("data", (chunk) => chunks.push(chunk));
        res.on("end", () => {
          const text = Buffer.concat(chunks).toString("utf-8");
          try {
            resolve(JSON.parse(text));
          } catch {
            resolve({ text });
          }
        });
      },
    );
    req.on("error", reject);
    signal?.addEventListener("abort", () => req.destroy(new Error("已停止")), { once: true });
    if (data) req.write(data);
    req.end();
  });
}

export default async function (pi: ExtensionAPI) {
  const setup = await request("GET", "/internal/pi/setup");
  for (const spec of setup.tools) {
    const fn = spec.function;
    pi.registerTool({
      name: fn.name,
      label: fn.name,
      description: fn.description,
      parameters: fn.parameters,
      async execute(toolCallId, params, signal) {
        const out = await request("POST", "/internal/pi/call", { id: toolCallId, name: fn.name, args: params }, signal);
        return { content: [{ type: "text", text: String(out.text ?? "") }], details: {} };
      },
    });
  }

  let turns = 0;
  pi.on("turn_end", async (event) => {
    if (event.toolResults.length === 0) return;
    turns += 1;
    let note = "";
    if (turns === setup.wrap_up_at) note = setup.wrap_up;
    if (turns >= setup.max_turns) {
      pi.setActiveTools([]);
      note = setup.final;
    }
    if (!note) return;
    return {
      entries: [...event.entries, { type: "custom_message", customType: "ledger-guard", content: note, display: false }],
    };
  });
}
