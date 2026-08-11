// DB 时间戳格式化：后端存的是 UTC（sqlite CURRENT_TIMESTAMP，无后缀），
// 前端统一转本地显示。今天 HH:mm，今年 MM-DD HH:mm，跨年 YYYY-MM-DD HH:mm。
export function formatDbTime(raw) {
  if (!raw) return '—';
  const s = String(raw).trim();
  // 已带时区/ISO 标记的直接解析；裸 'YYYY-MM-DD HH:MM:SS' 按 UTC 处理
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(s);
  const iso = s.includes('T') ? s : s.replace(' ', 'T');
  const d = new Date(hasTz ? iso : iso + 'Z');
  if (Number.isNaN(d.getTime())) return s;
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const hm = `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (d.toDateString() === now.toDateString()) return hm;
  if (d.getFullYear() === now.getFullYear()) {
    return `${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
  }
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${hm}`;
}
