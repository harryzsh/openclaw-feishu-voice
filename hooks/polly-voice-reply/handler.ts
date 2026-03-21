import { execSync } from "node:child_process";

const pollyVoiceReplyHook = async (event: any) => {
  // 只处理 agent 发出的消息
  if (event.type !== "message" || event.action !== "sent") {
    return;
  }

  try {
    const ctx = event.context;

    // 只处理飞书频道
    const channel = ctx?.channel || event?.channel;
    if (channel && channel !== "feishu") {
      return;
    }

    // 提取文字内容
    let fullText = "";
    if (ctx?.text) {
      fullText = ctx.text;
    } else if (ctx?.content) {
      if (typeof ctx.content === "string") {
        fullText = ctx.content;
      } else if (Array.isArray(ctx.content)) {
        fullText = ctx.content
          .filter((c: any) => c.type === "text")
          .map((c: any) => c.text)
          .join("\n");
      }
    }

    if (!fullText || fullText.trim() === "") return;

    // 超过 200 字不发语音
    if (fullText.length > 200) return;

    // 过滤系统消息
    const trimmed = fullText.trim();
    if (trimmed === "NO_REPLY" || trimmed === "HEARTBEAT_OK") return;

    const chatId = ctx?.chatId || ctx?.chat_id || process.env.FEISHU_CHAT_ID || "";
    const safeText = fullText.replace(/'/g, "'\\''");

    // 调用 polly-proxy 发语音
    execSync(
      `curl -s -X POST http://localhost:8080/v1/audio/speech \
        -H 'Content-Type: application/json' \
        -d '{"input":"${safeText}","voice":"Zhiyu","feishu_chat_id":"${chatId}"}'`,
      { timeout: 30000 }
    );
  } catch (err: any) {
    console.error("[polly-voice-reply] error:", err?.message);
  }
};

export default pollyVoiceReplyHook;
