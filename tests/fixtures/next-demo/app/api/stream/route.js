// 流式响应夹具（Locove 验收）：每 400ms 一段共 3 段，验证代理不缓冲
export async function GET() {
  const encoder = new TextEncoder();
  let i = 0;
  const stream = new ReadableStream({
    async pull(controller) {
      i += 1;
      controller.enqueue(encoder.encode(`chunk-${i}\n`));
      if (i >= 3) {
        controller.close();
        return;
      }
      await new Promise((r) => setTimeout(r, 400));
    },
  });
  return new Response(stream, {
    headers: { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" },
  });
}
