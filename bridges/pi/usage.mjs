/** Observe presence of native token counters without changing SDK payloads.
 * The pinned SDK initializes usage to zero even when a server omits it.
 * Each bridge owns one session, so the fetch wrapper is scoped to that run.
 * SSE bytes are forwarded unchanged, with bounded incremental inspection.
 */
export function observeNativeUsage(onUsage) {
  const original = globalThis.fetch;
  const fields = {
    input: ["prompt_tokens", "input_tokens", "promptTokens", "promptTokenCount"],
    output: ["completion_tokens", "output_tokens", "completionTokens", "candidatesTokenCount"],
    total: ["total_tokens", "totalTokens", "totalTokenCount"],
  };
  globalThis.fetch = async (...args) => {
    const response = await original(...args);
    if (!response.ok || !response.body ||
        !response.headers.get("content-type")?.includes("text/event-stream")) {
      return response;
    }
    let buffer = "";
    let overflow = false;
    const decoder = new TextDecoder();
    const inspect = (frame) => {
      const data = frame.split(/\r?\n/).filter((line) => line.startsWith("data:"))
        .map((line) => line.slice(5).trimStart()).join("\n");
      try {
        const event = JSON.parse(data);
        const candidates = [event.usage, event.message?.usage, event.response?.usage,
          event.usageMetadata, event.choices?.[0]?.usage];
        for (const [field, keys] of Object.entries(fields)) {
          for (const usage of candidates) {
            if (!usage) continue;
            for (const key of keys) {
              if (Object.hasOwn(usage, key) && Number.isFinite(usage[key]) && usage[key] >= 0) {
                onUsage(field, usage[key]);
                break;
              }
            }
          }
        }
      } catch { /* Protocol validation remains the SDK's responsibility. */ }
    };
    const body = response.body.pipeThrough(new TransformStream({
      transform(chunk, controller) {
        controller.enqueue(chunk);
        buffer += decoder.decode(chunk, { stream: true });
        let match;
        while ((match = /\r?\n\r?\n/.exec(buffer))) {
          if (!overflow) inspect(buffer.slice(0, match.index));
          buffer = buffer.slice(match.index + match[0].length);
          overflow = false;
        }
        if (buffer.length > 4_000_000) {
          buffer = buffer.slice(-3);
          overflow = true;
        }
      },
    }));
    return new Response(body, { status: response.status, statusText: response.statusText,
      headers: response.headers });
  };
  return () => { globalThis.fetch = original; };
}
