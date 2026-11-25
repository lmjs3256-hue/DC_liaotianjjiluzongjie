/**
 * dtt 取数后端（CDP 模式）：连接到用户已登录的「真人 Edge」浏览器，
 * 在其页面上下文里 fetch dtt API。Cloudflare 看到的是真人浏览器，不放行问题消失。
 *
 * 用法：
 *   node dtt_cdp.js probe                    # 探测连接/登录态/API
 *   node dtt_cdp.js fetch <start> <end>      # 取指定日期范围的 events + keywords + platforms
 *
 * 依赖：playwright-core（浏览器技能自带），Edge 需以 --remote-debugging-port=9222 启动。
 */
const path = require('path');
const fs = require('fs');

const SKILL_DIR = path.join(process.env.USERPROFILE || process.env.HOME || '', '.workbuddy', 'skills', 'playwright-browser-use');
let chromium;
try {
  ({ chromium } = require(path.join(SKILL_DIR, 'node_modules', 'playwright-core')));
} catch (e) {
  console.error(JSON.stringify({ ok: false, error: 'no playwright-core: ' + e.message }));
  process.exit(2);
}

const CDP = process.env.DTT_CDP_ENDPOINT || 'http://127.0.0.1:' + (process.env.DTT_CDP_PORT || '9444');
const ORIGIN = 'https://dtt.bigcatking.cc';

function out(obj) { process.stdout.write(JSON.stringify(obj)); }

async function connectPage() {
  const browser = await chromium.connectOverCDP(CDP);
  const ctx = browser.contexts()[0];
  if (!ctx) throw new Error('CDP 无浏览器上下文');
  // CDP 连接的 context 不支持 newPage(Target.createTarget)，必须复用现有页面。
  // 优先用已打开的 dtt 页面；否则取第一个页面 goto 到 dtt。
  let page = ctx.pages().find(p => { try { return p.url().includes('dtt.bigcatking.cc'); } catch (_) { return false; } });
  if (!page) page = ctx.pages()[0];
  if (!page) throw new Error('CDP 无可用页面（真人 Edge 未打开任何标签）');
  return { browser, ctx, page };
}

async function probe() {
  const { browser, page } = await connectPage();
  try {
    // 不主动 goto：真人 Edge 已停在登录后的 dtt 页面，刷新会触发校验跳 /login。
    if (!page.url().includes('dtt.bigcatking.cc')) {
      out({ ok: false, error: '当前页面非 dtt，请在真人 Edge 打开 dtt 登录页' });
      return;
    }
    const info = await page.evaluate(async () => {
      const sess = JSON.parse(localStorage.getItem('session') || 'null');
      const o = {
        href: location.href,
        title: document.title,
        hasSession: !!(sess && sess.token),
        webdriver: navigator.webdriver,
      };
      o.bodyHead = (document.body && document.body.innerText || '').slice(0, 120);
      if (sess && sess.token) {
        try {
          const r = await fetch('/api/events/list?limit=2&sortBy=ingested_at&sortOrder=DESC&startDate=2026-11-24&endDate=2026-11-25&useCursor=true', {
            headers: { Authorization: 'Bearer ' + sess.token }
          });
          o.apiStatus = r.status;
          o.contentType = r.headers.get('content-type') || '';
          // 只报结构信息，不打印响应正文（避免 token/敏感内容外泄）
          if (o.contentType.includes('json')) {
            const j = await r.json();
            o.apiJsonKeys = Object.keys(j || {});
            o.apiItems = ((j && j.data && j.data.items) || []).length;
          } else {
            o.nonJson = true;
          }
        } catch (e) { o.fetchErr = String(e); }
      }
      return o;
    });
    out({ ok: true, ...info });
  } finally {
    // CDP 连接是 attaches，不 close browser（否则会关掉用户的 Edge）
  }
}

async function fetchRange(start, end) {
  const { page } = await connectPage();
  if (!page.url().includes('dtt.bigcatking.cc')) {
    out({ ok: false, error: '当前页面非 dtt，请在真人 Edge 打开 dtt 并保持登录' });
    return;
  }
  // 从落盘凭证注入 session（不导航/不刷新，避免触发校验跳 /login）
  let inject = null;
  try {
    const f = path.join(process.env.USERPROFILE || '', '.pw-browser', 'dtt_session_localStorage.json');
    if (fs.existsSync(f)) inject = JSON.parse(fs.readFileSync(f, 'utf8'));
  } catch (_) {}
  const result = await page.evaluate(async (args) => {
    const S = args.start, E = args.end;
    if (args.inject) {
      localStorage.setItem('session', args.inject.session);
      if (args.inject.user) localStorage.setItem('user', args.inject.user);
    }
    const sess = JSON.parse(localStorage.getItem('session') || 'null');
    if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
      return { authExpired: true };
    }
    const H = { Authorization: 'Bearer ' + sess.token };
    const all = [];
    let ts = null, id = null, pages = 0, apiError = null;
    while (pages < 60) {
      let u = '/api/events/list?limit=100&sortBy=ingested_at&sortOrder=DESC' +
              '&startDate=' + S + '&endDate=' + E + '&useCursor=true';
      if (ts != null) {
        u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
      }
      let res;
      try { res = await fetch(u, { headers: H }); }
      catch (e) { apiError = 'network: ' + String(e); break; }
      if (res.status === 401) return { authExpired: true };
      const ct = res.headers.get('content-type') || '';
      if (!res.ok || ct.includes('text/html')) { apiError = 'HTTP ' + res.status + ' (' + ct + ')'; break; }
      const d = (await res.json()).data || {};
      const items = d.items || [];
      all.push(...items);
      pages++;
      if (!d.hasMore || !d.nextCursor || items.length === 0) break;
      ts = d.nextCursor.timestamp;
      id = d.nextCursor.id;
    }
    let kmap = {};
    try {
      const kwRes = await fetch('/api/keywords?limit=99999&offset=0', { headers: H });
      const kwCT = kwRes.headers.get('content-type') || '';
      if (kwCT.includes('json')) {
        const kwD = (await kwRes.json()).data || {};
        (kwD.items || []).forEach(k => {
          const sns = (k.groups || []).map(g => String(g.name || ''))
            .filter(n => /^sn\d+$/i.test(n)).map(n => parseInt(n.slice(2), 10))
            .filter(n => n >= 1 && n <= 512);
          if (sns.length) kmap[k.id] = Array.from(new Set(sns));
        });
      }
    } catch (_) {}
    let plats = [];
    try {
      const platRes = await fetch('/api/platforms', { headers: H });
      const pCT = platRes.headers.get('content-type') || '';
      if (pCT.includes('json')) plats = (await platRes.json()).data || [];
    } catch (_) {}
    return { ok: true, events: all, keywordSN: kmap, platforms: plats, pages, apiError };
  }, { start, end, inject });
  out(result);
}

async function snChannel(argsJson) {
  const { page } = await connectPage();
  if (!page.url().includes('dtt.bigcatking.cc')) {
    out({ ok: false, error: '当前页面非 dtt，请在真人 Edge 打开 dtt 并保持登录' });
    return;
  }
  let inject = null;
  try {
    const f = path.join(process.env.USERPROFILE || '', '.pw-browser', 'dtt_session_localStorage.json');
    if (fs.existsSync(f)) inject = JSON.parse(fs.readFileSync(f, 'utf8'));
  } catch (_) {}
  const args = JSON.parse(argsJson);
  args.inject = inject;
  const result = await page.evaluate(async (args) => {
    if (args.inject) {
      localStorage.setItem('session', args.inject.session);
      if (args.inject.user) localStorage.setItem('user', args.inject.user);
    }
    const sess = JSON.parse(localStorage.getItem('session') || 'null');
    if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) return { authExpired: true };
    const H = { Authorization: 'Bearer ' + sess.token };
    const out = { sn: args.sn };
    let kwIds = [];
    try {
      const kwRes = await fetch('/api/keywords?limit=99999&offset=0', { headers: H });
      const ct = kwRes.headers.get('content-type') || '';
      if (!ct.includes('json')) return { err: 'keywords HTTP ' + kwRes.status + ' (' + ct + ')' };
      const kwD = (await kwRes.json()).data || {};
      kwIds = (kwD.items || [])
        .filter(k => (k.groups || []).some(g => String(g.name || '').toLowerCase() === 'sn' + args.sn))
        .map(k => k.id);
    } catch (e) { return { err: 'keywords: ' + String(e) }; }
    out.keywordIds = kwIds;
    if (!kwIds.length) return { ok: true, events: [], keywordIds: [] };
    const all = [];
    const seen = new Set();
    for (const win of args.windows) {
      for (const kw of kwIds) {
        let ts = null, id = null, pages = 0;
        while (pages < 6) {
          let u = '/api/events/list?limit=100&sortBy=ingested_at&sortOrder=DESC&useCursor=true' +
                  '&keywordId=' + encodeURIComponent(kw) +
                  '&startDate=' + win.start + '&endDate=' + win.end +
                  '&platformId=' + args.platformId;
          if (ts != null) u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
          let res;
          try { res = await fetch(u, { headers: H }); }
          catch (e) { out.netErr = String(e); break; }
          if (res.status === 401) return { authExpired: true };
          const ct = res.headers.get('content-type') || '';
          if (!res.ok || !ct.includes('json')) { out.apiErr = 'HTTP ' + res.status + ' (' + ct + ')'; break; }
          const d = (await res.json()).data || {};
          const items = d.items || [];
          for (const e of items) {
            if (seen.has(e.id)) continue;
            seen.add(e.id); e._kw = kw; e._win = win.label; all.push(e);
          }
          pages++;
          if (!d.hasMore || !d.nextCursor || !items.length) break;
          ts = d.nextCursor.timestamp; id = d.nextCursor.id;
        }
      }
    }
    let platName = {};
    try {
      const pr = await fetch('/api/platforms', { headers: H });
      const pct = pr.headers.get('content-type') || '';
      if (pct.includes('json')) ((await pr.json()).data || []).forEach(p => { platName[p.id] = p.code; });
    } catch (e) {}
    all.forEach(e => { e.platform = platName[e.platformId] || e.platformId; });
    all.sort((a, b) => (b.occurredAt || 0) - (a.occurredAt || 0));
    return { ok: true, events: all, keywordIds: kwIds, platformName: args.platformId,
             platformId: args.platformId };
  }, args);
  out(result);
}

async function groupMessages(argsJson) {
  const { page } = await connectPage();
  if (!page.url().includes('dtt.bigcatking.cc')) {
    out({ ok: false, error: '当前页面非 dtt，请在真人 Edge 打开 dtt 并保持登录' });
    return;
  }
  let inject = null;
  try {
    const f = path.join(process.env.USERPROFILE || '', '.pw-browser', 'dtt_session_localStorage.json');
    if (fs.existsSync(f)) inject = JSON.parse(fs.readFileSync(f, 'utf8'));
  } catch (_) {}
  const args = JSON.parse(argsJson);
  args.inject = inject;
  const result = await page.evaluate(async (args) => {
    if (args.inject) {
      localStorage.setItem('session', args.inject.session);
      if (args.inject.user) localStorage.setItem('user', args.inject.user);
    }
    const sess = JSON.parse(localStorage.getItem('session') || 'null');
    if (!sess || !sess.token || (sess.expiresAt && Date.now() > sess.expiresAt)) {
      return { authExpired: true };
    }
    const H = { Authorization: 'Bearer ' + sess.token };
    const all = [];
    let apiError = null;
    for (const gid of args.groupIds) {
      let ts = null, id = null, pages = 0;
      const cap = Math.min(Number(args.maxPages) || 60, 60);
      while (pages < cap) {
        let u = '/api/group-messages?limit=' + args.limit +
                '&groupId=' + encodeURIComponent(gid) +
                '&startDate=' + args.start + '&endDate=' + args.end +
                '&sortBy=occurredAt&sortOrder=DESC&useCursor=true';
        if (ts != null) {
          u += '&cursorTimestamp=' + ts + '&cursorId=' + encodeURIComponent(id);
        }
        let res;
        try { res = await fetch(u, { headers: H }); }
        catch (e) { apiError = 'network gid=' + gid + ': ' + String(e); break; }
        if (res.status === 401) return { authExpired: true };
        const ct = res.headers.get('content-type') || '';
        if (!res.ok || !ct.includes('json')) {
          apiError = 'HTTP ' + res.status + ' (' + ct + ') gid=' + gid; break;
        }
        const d = (await res.json()).data || {};
        const items = d.items || [];
        all.push(...items);
        pages++;
        if (!d.hasMore || !d.nextCursor || !items.length) break;
        ts = d.nextCursor.timestamp;
        id = d.nextCursor.id;
      }
    }
    return { ok: true, events: all, pages: args.groupIds.length, apiError };
  }, args);
  out(result);
}

(async () => {
  const cmd = process.argv[2] || 'probe';
  try {
    if (cmd === 'probe') await probe();
    else if (cmd === 'fetch') await fetchRange(process.argv[3], process.argv[4]);
    else if (cmd === 'snchannel') await snChannel(process.argv[3]);
    else if (cmd === 'groupmessages') await groupMessages(process.argv[3]);
    else out({ ok: false, error: 'unknown cmd: ' + cmd });
  } catch (e) {
    out({ ok: false, error: String(e && e.message || e) });
  }
  // connectOverCDP 的 WebSocket 会保持事件循环活跃，必须显式退出，否则进程挂住。
  process.exit(0);
})();
