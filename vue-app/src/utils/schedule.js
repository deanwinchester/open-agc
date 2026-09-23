// 定时任务频率工具：友好表单 ↔ cron 表达式（存 UTC），及中文描述。
// 调度器（api/background.py）按 UTC 比较 next_run_at——cron 一律存 UTC；
// 界面输入/展示一律用本地时区，转换只在本文件发生。

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

// 本地「某天几点」→ UTC cron 的分钟/小时/日
function localToUtc(hour, minute, weekDay = null, monthDay = null) {
  const d = new Date();
  if (weekDay !== null) {
    // 对齐到本周目标星期（getDay: 0=周日）
    d.setDate(d.getDate() + ((weekDay - d.getDay() + 7) % 7));
  }
  if (monthDay !== null) {
    d.setDate(Math.min(monthDay, 28)); // 防 29-31 溢出当月；>28 的日期在下方按原值存
  }
  d.setHours(hour, minute, 0, 0);
  const out = { m: d.getUTCMinutes(), h: d.getUTCHours() };
  if (weekDay !== null) out.dow = d.getUTCDay();
  if (monthDay !== null) {
    // 月初早时段（如北京 1 号 00:30）换到 UTC 会落到上月最后一天——
    // 月级任务按「本地日期±1 天可接受」处理（跨月偏移属极端边角）
    out.dom = d.getUTCDate();
    if (out.dom !== monthDay && monthDay > 28) out.dom = monthDay;
  }
  return out;
}

/** 表单 → cron（UTC）。表单字段：mode/every/hour/minute/weekday/monthDay/raw */
export function formToCron(f) {
  if (f.mode === 'advanced') return String(f.raw || '').trim();
  if (f.mode === 'interval_min') return `*/${f.every} * * * *`;
  if (f.mode === 'interval_hour') return `0 */${f.every} * * *`;
  const c = localToUtc(f.hour ?? 9, f.minute ?? 0,
    f.mode === 'weekly' ? f.weekday : null,
    f.mode === 'monthly' ? f.monthDay : null);
  if (f.mode === 'daily') return `${c.m} ${c.h} * * *`;
  if (f.mode === 'weekly') return `${c.m} ${c.h} * * ${c.dow}`;
  if (f.mode === 'monthly') return `${c.m} ${c.h} ${c.dom} * *`;
  return '';
}

/** cron（UTC）→ 表单；不认识的表达式返回 null（走高级模式） */
export function cronToForm(cron) {
  const parts = String(cron || '').trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [m, h, dom, mon, dow] = parts;
  let mm = /^\*\/(\d+)$/.exec(m);
  if (mm && h === '*' && dom === '*' && mon === '*' && dow === '*') {
    return { mode: 'interval_min', every: Number(mm[1]) };
  }
  mm = /^0$/.test(m) && /^\*\/(\d+)$/.test(h) && dom === '*' && mon === '*' && dow === '*'
    ? /^\*\/(\d+)$/.exec(h) : null;
  if (mm) return { mode: 'interval_hour', every: Number(mm[1]) };
  if (!/^\d+$/.test(m) || !/^\d+$/.test(h)) return null;
  const um = Number(m), uh = Number(h);
  // UTC -> 本地：构造「本周该 UTC 时刻」的本地 Date 再读本地字段
  const d = new Date(Date.UTC(2026, 0, 5, uh, um)); // 2026-01-05 是周一
  if (dom === '*' && mon === '*' && dow === '*') {
    return { mode: 'daily', hour: d.getHours(), minute: d.getMinutes() };
  }
  if (dom === '*' && mon === '*' && /^\d+$/.test(dow)) {
    const ud = new Date(Date.UTC(2026, 0, 4 + Number(dow), uh, um)); // 1/4 是周日
    return { mode: 'weekly', hour: ud.getHours(), minute: ud.getMinutes(), weekday: ud.getDay() };
  }
  if (/^\d+$/.test(dom) && mon === '*' && dow === '*') {
    const md = new Date(Date.UTC(2026, 0, Number(dom), uh, um));
    return { mode: 'monthly', hour: md.getHours(), minute: md.getMinutes(), monthDay: md.getDate() };
  }
  return null;
}

/** cron → 中文一句话描述（不认识的返回原样） */
export function describeCron(cron) {
  const f = cronToForm(cron);
  if (!f) return String(cron || '');
  const hm = (x) => String(x).padStart(2, '0');
  const timeStr = `${hm(f.hour ?? 0)}:${hm(f.minute ?? 0)}`;
  switch (f.mode) {
    case 'interval_min': return `每 ${f.every} 分钟`;
    case 'interval_hour': return `每 ${f.every} 小时`;
    case 'daily': return `每天 ${timeStr}`;
    case 'weekly': return `每${WEEKDAYS[f.weekday]} ${timeStr}`;
    case 'monthly': return `每月 ${f.monthDay} 号 ${timeStr}`;
    default: return String(cron || '');
  }
}

/** UTC 'YYYY-MM-DD HH:MM:SS' → 本地显示 'MM-DD HH:mm'（今年省年份） */
export function utcToLocalShort(utcStr) {
  if (!utcStr) return '';
  const d = new Date(String(utcStr).replace(' ', 'T') + 'Z');
  if (Number.isNaN(d.getTime())) return utcStr;
  const p = (n) => String(n).padStart(2, '0');
  const now = new Date();
  const y = d.getFullYear() === now.getFullYear() ? '' : `${d.getFullYear()}-`;
  return `${y}${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
