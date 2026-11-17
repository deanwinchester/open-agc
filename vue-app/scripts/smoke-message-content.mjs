import assert from 'node:assert/strict';
import { isEmptyResponseContent } from '../src/utils/messageContent.js';

for (const content of [
  '',
  '   \n',
  '​',
  '（空）',
  '[System: Empty message content sanitised to satisfy protocol]',
  'System: Empty message content sanitised to satisfy protocol',
]) {
  assert.equal(isEmptyResponseContent(content), true, JSON.stringify(content));
}

for (const content of [
  '正在调用工具',
  'System: 这是一条真实的用户可见说明',
  '前缀 [System: Empty message content sanitised to satisfy protocol] 后缀',
  0,
]) {
  assert.equal(isEmptyResponseContent(content), false, JSON.stringify(content));
}

console.log('MESSAGE_CONTENT_SMOKE_OK');
