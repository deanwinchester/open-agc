// 模型空回复及其协议占位：这些内容不是有效助手回复，不应进入步骤卡片。
// LiteLLM 会把空字符串/纯空白文本替换为下面的可见占位；旧版 sanitizer
// 还使用「（空）」。零宽空格是当前 Anthropic 渠道使用的不可见协议占位。
const EMPTY_RESPONSE_PLACEHOLDERS = new Set([
  '（空）',
  '[System: Empty message content sanitised to satisfy protocol]',
  'System: Empty message content sanitised to satisfy protocol',
]);

export function isEmptyResponseContent(content) {
  const text = String(content ?? '').replace(/[​‌‍﻿]/g, '').trim();
  return !text || EMPTY_RESPONSE_PLACEHOLDERS.has(text);
}
