/* Dashboard → Config 页（vanilla ES2017，零依赖）.
 *
 * - 唯一全局 window.ConfigTab = {mount, refresh, isDirty}；DOM 一律 createElement / textContent；
 * - 数据形状对齐 GET / PUT /api/config，所有请求携带 X-Coding-Proxy-Admin；
 * - 纯函数（diff、币种、loc 映射、载荷构建）经 module.exports 导出，供 Node 测试。
 */
(function () {
  'use strict';

  // ── 纯函数 ────────────────────────────────────────────────
  var EDITABLE = ['vendors', 'tiers', 'failover', 'model_mapping', 'pricing', 'native_api', 'session_policies', 'antigravity_gateway'];
  var PRICE_FIELDS = ['input_cost_per_mtok', 'output_cost_per_mtok', 'cache_write_cost_per_mtok', 'cache_read_cost_per_mtok'];
  var PRICE_RE = /^([$¥])\s*(.+)$/;
  var TOKEN_RE = /^[A-Za-z0-9_-]{32,}$/;

  function isObj(v) { return v !== null && typeof v === 'object' && !Array.isArray(v); }
  function clone(v) { return v === undefined ? undefined : JSON.parse(JSON.stringify(v)); }
  function deepEqual(a, b) {
    if (a === b) return true;
    if (Array.isArray(a) || Array.isArray(b)) {
      if (!Array.isArray(a) || !Array.isArray(b) || a.length !== b.length) return false;
      return a.every(function (x, i) { return deepEqual(x, b[i]); });
    }
    if (!isObj(a) || !isObj(b)) return false;
    var ka = Object.keys(a), kb = Object.keys(b);
    return ka.length === kb.length && ka.every(function (k) {
      return Object.prototype.hasOwnProperty.call(b, k) && deepEqual(a[k], b[k]);
    });
  }
  function isTmpl(v) { return isObj(v) && Object.keys(v).length === 1 && typeof v.$tmpl === 'string'; }
  function validToken(t) { return typeof t === 'string' && TOKEN_RE.test(t); }

  function getIn(obj, parts) {
    var cur = obj;
    for (var i = 0; i < parts.length; i++) {
      if (cur === null || typeof cur !== 'object') return undefined;
      cur = cur[parts[i]];
    }
    return cur;
  }
  function setIn(obj, parts, value) {
    var cur = obj;
    for (var i = 0; i < parts.length - 1; i++) {
      if (!isObj(cur[parts[i]]) && !Array.isArray(cur[parts[i]])) cur[parts[i]] = {};
      cur = cur[parts[i]];
    }
    cur[parts[parts.length - 1]] = value;
  }
  // 删除叶子，并剪除 base 中不存在的空父对象（改回缺省值后不残留 {}）
  function delIn(obj, parts, base) {
    var chain = [obj];
    for (var i = 0; i < parts.length - 1; i++) {
      var next = chain[i][parts[i]];
      if (!isObj(next)) return;
      chain.push(next);
    }
    delete chain[chain.length - 1][parts[parts.length - 1]];
    for (var j = parts.length - 2; j >= 0; j--) {
      if (Object.keys(chain[j + 1]).length || getIn(base, parts.slice(0, j + 1)) !== undefined) break;
      delete chain[j][parts[j]];
    }
  }
  // 写入字段：base 未显式设置且新值等于 schema 缺省 → 删除键，避免「改回原值仍 dirty」
  function assignField(work, base, parts, value, dflt) {
    if (base && getIn(base, parts) === undefined && dflt !== undefined && deepEqual(value, dflt)) delIn(work, parts, base);
    else setIn(work, parts, value);
  }

  function parsePrice(v) {
    if (typeof v === 'number') return { currency: null, amount: String(v) };
    if (typeof v === 'string') {
      var m = PRICE_RE.exec(v.trim());
      if (m) return { currency: m[1] === '$' ? 'USD' : 'CNY', amount: m[2].trim() };
      return { currency: null, amount: v.trim() };
    }
    return { currency: null, amount: '0' };
  }
  // 与后端一致：首个带 $ / ¥ 前缀的单价决定整行币种，无前缀默认 USD
  function entryCurrency(entry) {
    for (var i = 0; i < PRICE_FIELDS.length; i++) {
      var p = parsePrice((entry || {})[PRICE_FIELDS[i]]);
      if (p.currency) return p.currency;
    }
    return 'USD';
  }
  function priceText(currency, amount) {
    var a = String(amount === undefined || amount === null ? '' : amount).trim();
    if (/^\d+$/.test(a)) a += '.0';
    return (currency === 'CNY' ? '¥' : '$') + a;
  }
  function withCurrency(entry, currency) {
    var out = clone(entry);
    PRICE_FIELDS.forEach(function (f) { out[f] = priceText(currency, parsePrice(out[f]).amount); });
    return out;
  }

  function locToPath(loc) {
    if (!Array.isArray(loc) || !loc.length) return '';
    if (loc[0] === 'secret_ops' && loc.length > 1) return String(loc[1]);
    return loc.map(String).join('.');
  }
  function pathCandidates(path) {
    var parts = path ? path.split('.') : [];
    var out = [];
    for (var i = parts.length; i > 0; i--) out.push(parts.slice(0, i).join('.'));
    return out;
  }
  function pillOf(path) {
    var head = String(path || '').split('.')[0];
    return EDITABLE.indexOf(head) >= 0 ? head : 'system';
  }

  function vendorMap(list) {
    var m = {};
    (list || []).forEach(function (v) { if (isObj(v)) m[v.vendor] = v; });
    return m;
  }
  function dirtySections(base, work) {
    return EDITABLE.filter(function (s) { return !deepEqual(base[s], work[s]); });
  }
  function countDiff(a, b) {
    if (deepEqual(a, b)) return 0;
    if (isObj(a) && isObj(b)) {
      var keys = Object.keys(a).concat(Object.keys(b).filter(function (k) { return !(k in a); }));
      return keys.reduce(function (n, k) { return n + countDiff(a[k], b[k]); }, 0);
    }
    if (Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every(isObj)) {
      return a.reduce(function (n, x, i) { return n + countDiff(x, b[i]); }, 0);
    }
    return 1;
  }
  function activeOps(ops) {
    return Object.keys(ops || {}).filter(function (p) { return ops[p] && ops[p].op !== 'keep'; });
  }
  function countChangedFields(base, work, ops) {
    var n = 0;
    dirtySections(base, work).forEach(function (s) {
      if (s !== 'vendors') { n += countDiff(base[s], work[s]); return; }
      var a = vendorMap(base.vendors), b = vendorMap(work.vendors);
      var types = Object.keys(a).concat(Object.keys(b).filter(function (t) { return !(t in a); }));
      var m = types.reduce(function (k, t) { return k + (t in a && t in b ? countDiff(a[t], b[t]) : 1); }, 0);
      n += m || 1; // 仅顺序变化
    });
    return n + activeOps(ops).length;
  }
  function buildPayload(revisions, base, work, ops) {
    var out = { revisions: {}, sections: {}, secret_ops: {} };
    dirtySections(base, work).forEach(function (s) {
      out.sections[s] = clone(work[s]);
      out.revisions[s] = revisions && typeof revisions[s] === 'number' ? revisions[s] : 0;
    });
    activeOps(ops).forEach(function (p) {
      out.secret_ops[p] = ops[p].op === 'set' ? { op: 'set', value: ops[p].value || '' } : { op: ops[p].op };
    });
    return out;
  }
  // 预测「目标地址变化 → 清空密钥」（服务端为准，仅用于确认框提示）：
  // 计入有存储记录（recorded）的密钥，以及本次保存将新建覆盖的 vendor——
  // 服务端会先物化其默认 env 密钥记录，再因目标地址变化将其清空
  function predictClearedSecrets(o) {
    var before = vendorMap(o.base.vendors), after = vendorMap(o.work.vendors), dflt = vendorMap(o.defaults.vendors);
    var target = function (v) {
      return ['base_url', 'token_url'].map(function (k) { return v && k in v ? v[k] : o.targetDefaults[k]; });
    };
    var overridden = function (v) { return v !== undefined && !deepEqual(v, dflt[v.vendor] || v); };
    var out = [];
    Object.keys(after).forEach(function (t) {
      if (deepEqual(target(before[t] || dflt[t]), target(after[t]))) return;
      if (deepEqual(target(after[t]), target(dflt[t]))) return; // 新目标即包内默认 → 服务端保留密钥
      var newlyOverridden = !overridden(before[t]) && overridden(after[t]);
      (o.secretFields[t] || []).forEach(function (f) {
        var p = 'vendors.' + t + '.' + f, op = (o.ops || {})[p], st = (o.secrets || {})[p];
        if (op && op.op !== 'keep') return;
        if (st && (st.recorded || (newlyOverridden && st.default_env && st.kind === 'env'))) out.push(p);
      });
    });
    return out;
  }
  // move(from, to) 后展开行的新下标：被移动行跟随自身，夹在区间内的其余行 ±1 平移
  function movedOpenIndex(open, from, to) {
    if (open === null || open === undefined) return open;
    if (open === from) return to;
    if (from < to && open > from && open <= to) return open - 1;
    if (to < from && open >= to && open < from) return open + 1;
    return open;
  }
  function enabledChain() {
    var m = vendorMap(S.work.vendors);
    return effectiveChain(S.work.tiers, S.work.vendors).filter(function (t) { return m[t] && m[t].enabled !== false; });
  }
  function effectiveChain(tiers, vendors) {
    if (Array.isArray(tiers)) return tiers.slice();
    return (vendors || []).filter(function (v) { return v.enabled !== false; }).map(function (v) { return v.vendor; });
  }
  function pricingKey(e) { return (e && e.vendor) + '\u0000' + (e && e.model); }
  function newDefaultRows(current, defaults) {
    var seen = {};
    (current || []).forEach(function (e) { seen[pricingKey(e)] = true; });
    return (defaults || []).filter(function (e) { return !seen[pricingKey(e)]; });
  }
  // 可合并的默认定价行：仅 pricing 已覆盖时；以已保存表为基准（未保存的删除不计入），已在工作副本中的不再提示
  function pricingMergeRows(overridden, base, work, defaults) {
    if ((overridden || []).indexOf('pricing') < 0) return [];
    return newDefaultRows(work, newDefaultRows(base, defaults));
  }

  var helpers = {
    deepEqual: deepEqual, clone: clone, isTmpl: isTmpl, validToken: validToken, assignField: assignField,
    parsePrice: parsePrice, entryCurrency: entryCurrency, priceText: priceText, withCurrency: withCurrency,
    locToPath: locToPath, pathCandidates: pathCandidates, pillOf: pillOf, dirtySections: dirtySections,
    countChangedFields: countChangedFields, buildPayload: buildPayload, predictClearedSecrets: predictClearedSecrets,
    movedOpenIndex: movedOpenIndex,
    effectiveChain: effectiveChain, newDefaultRows: newDefaultRows, pricingMergeRows: pricingMergeRows,
  };
  if (typeof module !== 'undefined' && module.exports) module.exports = helpers;
  if (typeof window === 'undefined' || typeof document === 'undefined') return;

  // ── 状态 ──────────────────────────────────────────────────
  var TOKEN_KEY = 'coding-proxy.admin-token';
  var PILLS = [
    ['vendors', 'Vendors'], ['tiers', 'Tiers'], ['failover', 'Failover'], ['model_mapping', 'Model Mapping'],
    ['pricing', 'Pricing'], ['native_api', 'Native API'], ['session_policies', 'Session Policies'], ['antigravity_gateway', 'Antigravity'], ['system', 'System'],
  ];
  var MODE_TEXT = {
    store: '配置存储正常（config.db）',
    defaults: '尚未初始化配置存储，当前使用包内默认值',
    legacy_readonly: '旧 YAML 与导入结果不一致，正在以旧 YAML 只读运行，不可在此修改',
    degraded: '存储的配置无效，代理请求暂停服务（503）；请修复下列错误后保存',
  };
  var S = {
    root: null, token: null, view: null, base: {}, work: {}, ops: {}, errors: [], notice: null,
    pill: 'vendors', vendor: null, expanded: {}, loaded: false, saving: false, readonly: false,
    specs: [], specMap: {}, schema: { fields: {}, groups: {} },
    retryOnly: null, fatalText: null,
  };
  var uid = 0;

  // ── DOM 工具 ──────────────────────────────────────────────
  function h(tag, props, kids) {
    var el = document.createElement(tag);
    Object.keys(props || {}).forEach(function (k) {
      var v = props[k];
      if (v === undefined || v === null || v === false) return;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = String(v);
      else if (k.slice(0, 2) === 'on') el.addEventListener(k.slice(2), v);
      else if (['value', 'checked', 'disabled', 'readOnly', 'selected'].indexOf(k) >= 0) el[k] = v;
      else el.setAttribute(k, v === true ? '' : String(v));
    });
    return add(el, kids);
  }
  function add(el, kids) {
    [].concat(kids === undefined ? [] : kids).forEach(function (k) {
      if (k === null || k === undefined || k === false) return;
      el.appendChild(typeof k === 'object' ? k : document.createTextNode(String(k)));
    });
    return el;
  }
  function clear(el) { while (el.firstChild) el.removeChild(el.firstChild); }
  function btn(text, onclick, cls, extra) {
    return h('button', Object.assign({ type: 'button', class: 'cfg-btn ' + (cls || ''), text: text, onclick: onclick }, extra || {}));
  }
  function badge(text, kind, title) {
    return h('span', { class: 'status-badge cfg-trunc sb-' + (kind || 'info'), text: text, title: title });
  }
  function fmtVal(v) {
    if (v === null || v === undefined || v === '') return '—';
    if (Array.isArray(v)) return v.length ? v.map(fmtVal).join(', ') : '[]';
    if (isTmpl(v)) return v.$tmpl;
    return typeof v === 'object' ? JSON.stringify(v) : String(v);
  }
  function spec(path) { return S.specMap[path] || { path: path, label: path, widget: 'text', description: '' }; }
  function dflt(specPath) { return specPath in S.schema.groups ? null : S.schema.fields[specPath]; }
  function findVendor(list, t) { return vendorMap(list)[t]; }
  function vendorTypes() { return spec('tiers').enum || []; }

  // ── Admin token ───────────────────────────────────────────
  function storageGet() { try { return window.localStorage.getItem(TOKEN_KEY); } catch (e) { return null; } }
  function storageSet(v) {
    try { if (v) window.localStorage.setItem(TOKEN_KEY, v); else window.localStorage.removeItem(TOKEN_KEY); } catch (e) { /* 存储不可用时仅保留内存态 */ }
  }
  function captureFragmentToken() {
    var m = /(?:^#|&)admin=([^&]*)/.exec(window.location.hash || '');
    if (!m) return;
    var t = '';
    try { t = decodeURIComponent(m[1]); } catch (e) { /* ignore */ }
    if (validToken(t)) { S.token = t; storageSet(t); }
    try { window.history.replaceState(window.history.state, '', window.location.pathname + window.location.search); } catch (e) { /* ignore */ }
  }
  function token() { return S.token || storageGet(); }
  function forgetToken() { S.token = null; storageSet(null); }

  function request(method, body) {
    var headers = { 'X-Coding-Proxy-Admin': token() || '' };
    var init = { method: method, headers: headers, cache: 'no-store', credentials: 'same-origin' };
    if (body !== undefined) { headers['Content-Type'] = 'application/json'; init.body = JSON.stringify(body); }
    return fetch('/api/config', init).then(function (res) {
      return res.json().catch(function () { return {}; }).then(function (data) { return { status: res.status, data: data || {} }; });
    });
  }
  function errText(data) {
    var e = data && (data.message || data.error);
    return typeof e === 'string' ? e : (e && e.message) || '';
  }

  // ── 加载 ──────────────────────────────────────────────────
  // 返回 Promise<true|false|'locked'>（成功 adopt 为 true；'locked' 已渲染解锁界面；
  // false 为致命失败并已 renderFatal）——调用方据 false 走 fatal 分支
  function load(only) {
    if (!validToken(token())) { renderLocked(); return Promise.resolve('locked'); }
    return request('GET').then(function (r) {
      if (r.status === 401) { forgetToken(); renderLocked('管理 token 无效或已更换，请重新解锁'); return 'locked'; }
      if (r.status !== 200) { renderFatal('加载配置失败（HTTP ' + r.status + '）' + errText(r.data)); return false; }
      adopt(r.data, only);
      S.loaded = true;
      S.retryOnly = null;
      rerender();
      return true;
    }, function () { renderFatal('加载配置失败：网络错误'); return false; });
  }
  function adopt(view, only) {
    var prev = S.view;
    if (only && prev) {
      only.forEach(function (s) {
        if (EDITABLE.indexOf(s) < 0) return;
        S.base[s] = clone(view.values[s]);
        S.work[s] = clone(view.values[s]);
        prev.values[s] = view.values[s];
        prev.revisions[s] = view.revisions[s];
      });
      ['mode', 'errors', 'notices', 'overridden', 'vendor_overrides', 'secrets', 'sources', 'running', 'apply_supported']
        .forEach(function (k) { prev[k] = view[k]; });
      prev.values.absent_vendors = view.values.absent_vendors;
    } else {
      S.view = view;
      S.base = {};
      S.work = {};
      S.ops = {};
      EDITABLE.forEach(function (s) { S.base[s] = clone(view.values[s]); S.work[s] = clone(view.values[s]); });
      S.errors = (view.errors || []).slice();
    }
    S.specs = (view.registry && view.registry.fields) || [];
    S.specMap = {};
    S.specs.forEach(function (sp) { S.specMap[sp.path] = sp; });
    S.readonly = S.view.mode === 'legacy_readonly';
  }

  // ── 渲染骨架 ──────────────────────────────────────────────
  function rerender() {
    if (!S.root || !S.view) return;
    var nodes = [renderToolbar()].concat(renderBanners());
    var body = h('div', { class: 'card cfg-body', 'data-path': S.pill === 'system' ? null : S.pill });
    RENDER[S.pill](body);
    nodes.push(body, renderSaveBar());
    clear(S.root);
    add(S.root, nodes);
    applyErrors();
    updateDirty();
  }
  function renderToolbar() {
    // pills 为按钮组（非 tabs 模式）：当前项以 aria-current 标注
    var pills = h('div', { class: 'cfg-pills', role: 'group', 'aria-label': 'Config sections' });
    PILLS.forEach(function (p) {
      pills.appendChild(h('button', {
        type: 'button', class: 'range-btn' + (S.pill === p[0] ? ' active' : ''), 'data-pill': p[0], text: p[1],
        'aria-current': S.pill === p[0] ? 'true' : null, onclick: function () { goPill(p[0]); },
      }));
    });
    return h('div', { class: 'cfg-toolbar' }, [
      pills,
      btn('锁定', function () {
        if (isDirty() && !window.confirm('锁定会丢弃未保存的修改，继续？')) return;
        forgetToken();
        renderLocked();
      }, 'cfg-btn-sm', { title: '清除本浏览器保存的管理 token' }),
    ]);
  }
  function goPill(name, vendor) {
    S.pill = name;
    if (vendor) S.vendor = vendor;
    syncUrl();
    rerender();
  }
  function syncUrl() {
    try {
      var url = new URL(window.location.href);
      url.searchParams.set('section', S.pill === 'vendors' && S.vendor ? 'vendors.' + S.vendor : S.pill);
      window.history.replaceState(window.history.state, '', url);
    } catch (e) { /* ignore */ }
  }
  function readUrlSection() {
    try {
      var parts = (new URL(window.location.href).searchParams.get('section') || '').split('.');
      if (PILLS.some(function (p) { return p[0] === parts[0]; })) S.pill = parts[0];
      if (parts[0] === 'vendors' && parts[1]) S.vendor = parts[1];
    } catch (e) { /* ignore */ }
  }
  function banner(kind, text, extra) {
    return h('div', { class: 'cfg-banner cfg-banner-' + kind, role: kind === 'err' ? 'alert' : 'status' }, [h('div', { class: 'cfg-banner-text' }, text)].concat(extra || []));
  }
  function noticeBanner(onClose) {
    return banner(S.notice.kind, [S.notice.text].concat((S.notice.lines || []).map(function (l) { return h('div', { class: 'cfg-note', text: l }); })),
      [btn('关闭', function () { S.notice = null; (onClose || rerender)(); }, 'cfg-btn-sm')]);
  }
  function renderBanners() {
    var out = [], v = S.view;
    if (S.notice) out.push(noticeBanner());
    if (v.mode !== 'store') out.push(banner(v.mode === 'defaults' ? 'info' : 'err', MODE_TEXT[v.mode] || v.mode));
    if (!v.apply_supported && !S.readonly) out.push(banner('info', '当前实例不支持在线生效：保存会写入 config.db，需重启 coding-proxy 后生效'));
    var boot = (v.running && v.running.boot_revisions) || {};
    var pending = EDITABLE.filter(function (s) { return s in boot && boot[s] !== v.revisions[s]; });
    if (pending.length) out.push(banner('warn', '以下 section 已保存但尚未在运行实例生效：' + pending.join(', ')));
    return out;
  }
  function renderLocked(reason) {
    if (!S.root) return;
    S.loaded = false;
    S.view = null; // 丢弃工作副本（含待设置的密钥明文）
    S.ops = {};
    updateDirty(); // 清除 Config 页签上的 dirty 点
    var input = h('input', {
      type: 'password', class: 'cfg-input', autocomplete: 'off', 'aria-label': 'Admin token',
      placeholder: '粘贴 coding-proxy config token 的输出',
      onkeydown: function (e) { if (e.key === 'Enter') unlock(); },
    });
    var err = h('div', { class: 'cfg-err', role: 'alert' });
    function unlock() {
      var t = input.value.trim();
      if (!validToken(t)) { err.textContent = 'token 格式无效（至少 32 位 URL-safe 字符）'; return; }
      input.value = '';
      S.token = t;
      storageSet(t);
      load();
    }
    clear(S.root);
    S.root.appendChild(h('div', { class: 'card cfg-lock' }, [
      h('div', { class: 'cfg-section-title', text: 'Admin Token' }),
      h('p', { class: 'cfg-note', text: reason || '读写配置需要管理 token（防止同机其他进程篡改配置）。请使用 coding-proxy start 打印的 Dashboard 链接打开，或在终端运行 coding-proxy config token 并粘贴输出。' }),
      h('div', { class: 'cfg-lock-row' }, [input, btn('解锁', unlock, 'cfg-btn-primary')]),
      err,
    ]));
    input.focus();
  }
  function renderFatal(text) {
    if (!S.root) return;
    S.fatalText = text;
    clear(S.root);
    // 保留保存结果提示：PUT 已成功而重载失败时，不让「加载失败」掩盖「已保存」
    if (S.notice) { var nb = noticeBanner(function () { nb.remove(); }); S.root.appendChild(nb); }
    S.root.appendChild(banner('err', text, [btn('重试', function () { retryLoad(); }, 'cfg-btn-sm')]));
  }

  // 重试按失败时的定向范围加载（409 冲突重载失败 → 仅重载冲突 section，保住其余本地修改）
  function retryLoad() {
    var only = S.retryOnly;
    if (only) S.notice = null; // 「重新加载失败」的提示在重试后不再成立
    // 失败时保留 only：再次点「重试」仍仅重载冲突 section，其余本地修改不丢
    return load(only);
  }

  // ── 表单行与控件 ──────────────────────────────────────────
  function infoTip(label, desc) {
    var id = 'cfg-tip' + (++uid);
    // Esc 隐藏全部说明；再次悬停或聚焦某个 ⓘ 时恢复该项
    var rearm = function (e) { e.currentTarget.classList.remove('cfg-tip-off'); };
    return h('span', { class: 'cfg-info-wrap', onmouseenter: rearm, onfocusin: rearm }, [
      h('button', { type: 'button', class: 'cfg-info', 'aria-label': 'Help: ' + label, 'aria-describedby': id, text: 'ⓘ' }),
      h('span', { class: 'cfg-tip', role: 'tooltip', id: id, text: desc || label }),
    ]);
  }
  var LABELABLE = ['INPUT', 'SELECT', 'TEXTAREA', 'BUTTON', 'OUTPUT', 'METER', 'PROGRESS'];
  function formRow(sp, path, build, extras) {
    var id = 'cfg-f' + (++uid), lid = id + '-label';
    var ctrl = build(id);
    var target = ctrl && (ctrl.id === id ? ctrl : ctrl.querySelector('#' + id));
    // 只读值 / 无可编辑控件时 <label for> 无关联目标：改由 group + aria-labelledby 命名值区域
    var labelable = !!target && LABELABLE.indexOf(target.tagName) >= 0;
    return h('div', { class: 'cfg-row', 'data-path': path }, [
      labelable
        ? h('label', { class: 'cfg-label cfg-trunc', for: id, text: sp.label })
        : h('span', { class: 'cfg-label cfg-trunc', id: lid, text: sp.label }),
      infoTip(sp.label, sp.description),
      h('div', { class: 'cfg-control', role: labelable ? null : 'group', 'aria-labelledby': labelable ? null : lid },
        [ctrl, sp.unit ? h('span', { class: 'cfg-unit', text: sp.unit }) : null].concat(extras || [])),
    ]);
  }
  // 绑定：container() 返回被编辑对象（work），baseC() 返回对应 base；parts 为相对路径
  function bindObj(container, baseC, parts, path, specPath) {
    return {
      path: path,
      get: function () { var v = getIn(container(), parts); return v === undefined ? dflt(specPath) : v; },
      set: function (v) { var c = container(); if (!c) return; assignField(c, baseC(), parts, v, dflt(specPath)); changed(); },
    };
  }
  function bindRoot(parts, specPath) {
    return bindObj(function () { return S.work; }, function () { return S.base; }, parts, parts.join('.'), specPath || parts.join('.'));
  }
  function numVal(s) { if (s === '') return null; var n = Number(s); return isFinite(n) ? n : s; }
  function sourceBadges(path, value) {
    var src = (S.view.sources || {})[path], out = [];
    if (isTmpl(value)) out.push(badge('Template', 'info', '该值为环境变量模板，仅可经 coding-proxy config import 修改'));
    if (src === 'env') out.push(badge('Env override', 'warn', '运行时被环境变量覆盖；该变量存在时此处的值不生效'));
    if (src === 'cli') out.push(badge('CLI', 'warn', '本次启动被命令行参数覆盖'));
    return out;
  }
  function field(sp, b, extraLock) {
    var v = b.get(), dis = S.readonly || sp.readonly || extraLock;
    return formRow(sp, b.path, function (id) { return control(sp, b, id, dis); }, sourceBadges(b.path, v));
  }
  function control(sp, b, id, dis) {
    var v = b.get();
    if (isTmpl(v)) return h('input', { id: id, class: 'cfg-input', type: 'text', value: v.$tmpl, readOnly: true });
    if (sp.widget === 'bool') {
      return h('input', { id: id, type: 'checkbox', class: 'cfg-check', checked: !!v, disabled: dis, onchange: function (e) { b.set(e.target.checked); } });
    }
    if (sp.widget === 'number') {
      return h('input', {
        id: id, type: 'number', step: 'any', min: sp.min, max: sp.max, class: 'cfg-input cfg-num', disabled: dis,
        value: v === null || v === undefined ? '' : String(v), oninput: function (e) { b.set(b.raw ? e.target.value : numVal(e.target.value)); },
      });
    }
    if (sp.widget === 'enum') {
      var opts = (sp.enum || []).slice();
      if (v !== undefined && v !== null && opts.indexOf(v) < 0) opts.push(v);
      return h('select', { id: id, class: 'cfg-input', disabled: dis, onchange: function (e) { b.set(e.target.value); } },
        opts.map(function (o) { return h('option', { value: o, text: b.optLabel ? b.optLabel(o) : o, selected: o === v }); }));
    }
    if (sp.widget === 'list') return listWidget(sp, b, id, dis);
    if (sp.widget === 'map') return mapWidget(sp, b, id, dis);
    if (sp.widget === 'secret') return secretWidget(sp, b.path, id);
    return h('input', {
      id: id, type: 'text', class: 'cfg-input', spellcheck: 'false', disabled: dis,
      value: v === null || v === undefined ? '' : String(v), oninput: function (e) { b.set(e.target.value); },
    });
  }
  function listWidget(sp, b, id, dis) {
    var wrap = h('div', { class: 'cfg-chips' });
    var numeric = /status_codes$/.test(sp.path);
    function cur() { var v = b.get(); return Array.isArray(v) ? v : []; }
    function commit(next) { b.set(next); redraw(); var a = wrap.querySelector('.cfg-chip-add'); if (a) a.focus(); }
    function addText(input) {
      var t = input.value.trim();
      if (!t) return;
      var v = numeric ? Number(t) : t;
      if (numeric && !Number.isInteger(v)) { input.classList.add('cfg-bad'); return; }
      if (cur().indexOf(v) < 0) commit(cur().concat([v]));
      else input.value = '';
    }
    function redraw() {
      clear(wrap);
      cur().forEach(function (item, i) {
        wrap.appendChild(h('span', { class: 'cfg-chip' }, [
          h('span', { class: 'cfg-chip-text cfg-trunc', text: String(item) }),
          dis ? null : h('button', {
            type: 'button', class: 'cfg-chip-x', text: '×', 'aria-label': '移除 ' + item,
            onclick: function () { var n = cur().slice(); n.splice(i, 1); commit(n); },
          }),
        ]));
      });
      if (dis) return;
      if (sp.enum) {
        var left = sp.enum.filter(function (o) { return cur().indexOf(o) < 0; });
        wrap.appendChild(h('select', {
          id: id, class: 'cfg-input cfg-chip-add', onchange: function (e) { if (e.target.value) commit(cur().concat([e.target.value])); },
        }, [h('option', { value: '', text: '+ 添加' })].concat(left.map(function (o) { return h('option', { value: o, text: o }); }))));
      } else {
        wrap.appendChild(h('input', {
          id: id, type: 'text', class: 'cfg-input cfg-chip-add', placeholder: '输入后回车添加', spellcheck: 'false',
          onkeydown: function (e) { if (e.key === 'Enter') { e.preventDefault(); addText(e.target); } },
          onblur: function (e) { addText(e.target); },
        }));
      }
    }
    redraw();
    return wrap;
  }
  function mapWidget(sp, b, id, dis) {
    var wrap = h('div', { class: 'cfg-map' });
    function cur() { var v = b.get(); return isObj(v) ? clone(v) : {}; }
    function redraw() {
      clear(wrap);
      Object.keys(cur()).forEach(function (k) {
        wrap.appendChild(h('div', { class: 'cfg-map-row' }, [
          h('span', { class: 'cfg-map-key cfg-trunc', text: k }),
          h('input', {
            type: 'number', class: 'cfg-input cfg-num', value: String(cur()[k]), disabled: dis, 'aria-label': k,
            oninput: function (e) { var n = cur(); n[k] = numVal(e.target.value); b.set(n); },
          }),
          dis ? null : btn('删除', function () { var n = cur(); delete n[k]; b.set(n); redraw(); }, 'cfg-btn-sm'),
        ]));
      });
      if (dis) return;
      var key = h('input', { id: id, type: 'text', class: 'cfg-input cfg-map-key', placeholder: '模型名', spellcheck: 'false' });
      var val = h('input', { type: 'number', class: 'cfg-input cfg-num', placeholder: '上限', 'aria-label': '上限' });
      wrap.appendChild(h('div', { class: 'cfg-map-row' }, [key, val, btn('添加', function () {
        var k = key.value.trim();
        if (!k || val.value === '') return;
        var n = cur(); n[k] = numVal(val.value); b.set(n); redraw();
      }, 'cfg-btn-sm')]));
    }
    redraw();
    return wrap;
  }
  function secretPill(st, op, sp) {
    var kind = 'muted', text = 'Empty', title = sp.unset_hint ? '未设置：' + sp.unset_hint : '未设置';
    if (op) {
      kind = 'info';
      text = op.op === 'set' ? 'Pending: Set' : op.op === 'clear' ? 'Pending: Clear' : 'Pending: Env ' + st.default_env;
      title = '保存后生效';
    } else if (st.kind === 'stored') {
      kind = st.resolved ? 'ok' : 'err';
      text = st.resolved ? 'Stored' + (st.hint ? ' ' + st.hint : '') : 'Unresolved';
      title = st.resolved ? '已加密保存在 config.db' : '无法解密：master key 与存储不匹配';
    } else if (st.kind === 'env') {
      kind = st.resolved ? 'ok' : 'warn';
      text = 'Env ' + st.env_var + (st.hint ? ' ' + st.hint : '');
      title = st.resolved ? '读取环境变量 ' + st.env_var : '环境变量 ' + st.env_var + ' 未设置' + (sp.unset_hint ? '；' + sp.unset_hint : '');
    } else if (st.kind === 'default') {
      text = 'Default';
      title = sp.unset_hint || '使用内置默认值';
    }
    return badge(text, kind, title);
  }
  function setOp(path, kind) {
    if (!kind || kind === 'keep') delete S.ops[path];
    else S.ops[path] = kind === 'set' ? { op: 'set', value: '' } : { op: kind };
    changed();
    rerender();
    var input = kind === 'set' && S.root.querySelector('.cfg-secret-input[data-for="' + path + '"]');
    if (input) input.focus();
  }
  function secretWidget(sp, path, id) {
    var st = (S.view.secrets || {})[path] || { kind: 'empty' }, op = S.ops[path];
    var wrap = h('div', { class: 'cfg-secret' }, [secretPill(st, op, sp)]);
    if (S.readonly || sp.readonly) return wrap;
    wrap.appendChild(h('select', {
      id: id, class: 'cfg-input cfg-secret-menu', onchange: function (e) { var k = e.target.value; e.target.value = ''; if (k) setOp(path, k); },
    }, [
      h('option', { value: '', text: '更改 ▾' }),
      h('option', { value: 'set', text: '设置新值' }),
      st.default_env ? h('option', { value: 'default_env', text: '使用默认环境变量 ' + st.default_env }) : null,
      h('option', { value: 'clear', text: '清空' }),
      op ? h('option', { value: 'keep', text: '撤销更改' }) : null,
    ]));
    if (op && op.op === 'set') {
      wrap.appendChild(h('input', {
        type: 'password', class: 'cfg-input cfg-secret-input', autocomplete: 'new-password', 'data-for': path,
        'aria-label': sp.label + ' 新值', placeholder: '输入新值（保存后不可查看）', value: op.value,
        oninput: function (e) { op.value = e.target.value; changed(); },
      }));
    }
    return wrap;
  }
  function group(title, rows, cls) {
    return h('div', { class: 'cfg-group ' + (cls || '') }, [title ? h('div', { class: 'cfg-group-title', text: title }) : null].concat(rows));
  }
  function sectionHead(title, sp, actions) {
    return h('div', { class: 'cfg-section-head' }, [
      h('div', { class: 'cfg-section-title' }, [h('span', { text: title }), sp ? infoTip(title, sp.description) : null]),
      h('div', { class: 'cfg-actions' }, actions || []),
    ]);
  }
  function resetBtn(section) {
    if (S.readonly) return null;
    return btn('恢复默认', function () { S.work[section] = clone(S.view.defaults[section]); changed(); rerender(); }, 'cfg-btn-sm',
      { title: '把本 section 改回包内默认值（保存后生效）' });
  }

  // ── 表格 ──────────────────────────────────────────────────
  function table(cols, rows, path) {
    var cg = h('colgroup');
    cols.forEach(function (c) { var col = h('col'); col.style.width = 'var(' + c.w + ')'; cg.appendChild(col); });
    cg.appendChild(h('col', { class: 'cfg-col-fill' }));
    var head = h('tr', {}, cols.map(function (c) { return h('th', { scope: 'col', class: 'cfg-trunc', text: c.label }); }).concat([h('th', { 'aria-hidden': 'true' })]));
    return h('div', { class: 'ft-table-wrap', 'data-path': path }, h('table', { class: 'cfg-table' }, [cg, h('thead', {}, head), h('tbody', {}, rows)]));
  }
  function cell(text, cls) { return h('td', { class: 'cfg-trunc ' + (cls || ''), text: fmtVal(text) }); }
  function orderCell(arr, i, onMove) {
    return h('td', { class: 'cfg-cell-actions' }, [
      btn('↑', function (e) { e.stopPropagation(); onMove(i, i - 1); }, 'cfg-btn-icon', { 'aria-label': '上移', disabled: S.readonly || i === 0 }),
      btn('↓', function (e) { e.stopPropagation(); onMove(i, i + 1); }, 'cfg-btn-icon', { 'aria-label': '下移', disabled: S.readonly || i === arr.length - 1 }),
    ]);
  }
  // 列表 section：固定列宽表格 + 展开行编辑 +（可选）↑/↓ 排序
  function listBlock(o) {
    var arr = function () { var a = getIn(S.work, o.parts); return Array.isArray(a) ? a : []; };
    var setArr = function (a, open) { setIn(S.work, o.parts, a); S.expanded[o.key] = open; changed(); rerender(); };
    var prefix = o.parts.join('.');
    var cols = (o.ordered ? [{ label: 'Order', w: '--col-order' }] : []).concat(o.cols, [{ label: 'Actions', w: '--col-actions' }]);
    var move = function (from, to) {
      var a = arr().slice(), item = a.splice(from, 1)[0];
      a.splice(to, 0, item);
      setArr(a, movedOpenIndex(S.expanded[o.key], from, to));
    };
    var rows = [];
    arr().forEach(function (item, i) {
      var open = S.expanded[o.key] === i;
      var cells = o.cols.map(function (c) { return cell(c.text(item), c.mono ? 'cfg-mono' : ''); });
      var refresh = function () { var it = arr()[i]; o.cols.forEach(function (c, k) { cells[k].textContent = fmtVal(c.text(it)); }); };
      var toggle = function () { S.expanded[o.key] = open ? null : i; rerender(); };
      rows.push(h('tr', { class: 'cfg-list-row' + (open ? ' cfg-row-open' : ''), 'data-path': prefix + '.' + i, onclick: toggle },
        (o.ordered ? [orderCell(arr(), i, move)] : []).concat(cells, [h('td', { class: 'cfg-cell-actions' }, [
          btn(open ? '收起' : '编辑', function (e) { e.stopPropagation(); toggle(); }, 'cfg-btn-sm', { 'aria-expanded': open ? 'true' : 'false' }),
          S.readonly ? null : btn('删除', function (e) { e.stopPropagation(); var a = arr().slice(); a.splice(i, 1); setArr(a, null); }, 'cfg-btn-sm cfg-btn-danger'),
        ]), h('td')])));
      if (!open) return;
      var fields = o.fields(i).map(function (f) {
        var b = f.bind, set = b.set;
        b.set = function (v) { set(v); refresh(); };
        return f.row ? f.row(b) : field(f.spec, b);
      });
      rows.push(h('tr', { class: 'cfg-detail-row' }, h('td', { colspan: String(cols.length + 1) }, h('div', { class: 'detail-card cfg-detail' }, fields))));
    });
    if (!rows.length) rows.push(h('tr', {}, h('td', { colspan: String(cols.length + 1), class: 'empty', text: '暂无条目' })));
    var actions = (o.actions || []).concat(S.readonly ? [] : [btn('添加', function () { var a = arr().slice(); a.push(o.blank()); setArr(a, a.length - 1); }, 'cfg-btn-sm')]);
    return h('div', { class: 'cfg-list' }, [sectionHead(o.title, spec(o.specPath), actions)].concat(o.before || [], [table(cols, rows, prefix)]));
  }
  function itemBind(parts, i, rel, specPath) {
    return bindObj(function () { return (getIn(S.work, parts) || [])[i]; }, function () { return (getIn(S.base, parts) || [])[i]; },
      rel.split('.'), parts.concat([i, rel]).join('.'), specPath);
  }
  function itemFields(parts, i, rels) {
    var sp = parts.join('.') + '.*.';
    return rels.map(function (rel) { return { spec: spec(sp + rel), bind: itemBind(parts, i, rel, sp + rel) }; });
  }

  // ── Sections ──────────────────────────────────────────────
  var RENDER = {};
  function vendorDirty(t) {
    var p = 'vendors.' + t + '.';
    return !deepEqual(findVendor(S.base.vendors, t), findVendor(S.work.vendors, t)) || activeOps(S.ops).some(function (k) { return k.indexOf(p) === 0; });
  }
  function errorAt(prefix) {
    return S.errors.some(function (e) { var p = locToPath(e.loc); return p === prefix || p.indexOf(prefix + '.') === 0; });
  }
  RENDER.vendors = function (body) {
    var present = vendorMap(S.work.vendors);
    var types = (S.work.vendors || []).map(function (v) { return v.vendor; });
    vendorTypes().forEach(function (t) { if (types.indexOf(t) < 0) types.push(t); });
    if (types.indexOf(S.vendor) < 0) S.vendor = types[0];
    body.appendChild(h('div', { class: 'cfg-subpills', role: 'group', 'aria-label': 'Vendors' }, types.map(function (t) {
      var v = present[t];
      var cls = 'range-btn' + (S.vendor === t ? ' active' : '') + (!v || v.enabled === false ? ' cfg-muted' : '') +
        (vendorDirty(t) ? ' cfg-has-dot' : '') + (errorAt('vendors.' + t) ? ' cfg-has-err' : '');
      return h('button', { type: 'button', class: cls, text: t, 'data-vendor': t, 'aria-current': S.vendor === t ? 'true' : null, onclick: function () { goPill('vendors', t); } });
    }).concat([h('span', { class: 'cfg-spacer' }), resetBtn('vendors')])));
    var t = S.vendor, vendor = present[t];
    if (!vendor) {
      body.appendChild(group(null, [
        sectionHead(t, null, [S.readonly ? null : btn('添加', function () { addVendor(t); }, 'cfg-btn-primary cfg-btn-sm')]),
        badge('Not configured', 'muted'),
        h('p', { class: 'cfg-note', text: '该 vendor 未在配置中，不参与路由。添加后以包内默认参数开始，可在此调整。' }),
      ]));
      return;
    }
    body.appendChild(vendorHeader(t, vendor));
    var groups = [], byName = {};
    S.specs.forEach(function (sp) {
      if (sp.path.indexOf('vendors.*.') !== 0 || (sp.vendors && sp.vendors.indexOf(t) < 0)) return;
      var g = sp.group || 'General';
      if (!byName[g]) { byName[g] = []; groups.push(g); }
      byName[g].push(sp);
    });
    var container = function () { return findVendor(S.work.vendors, t); };
    var baseC = function () { return findVendor(S.base.vendors, t); };
    var vb = function (sp) {
      var rel = sp.path.slice('vendors.*.'.length).split('.');
      return bindObj(container, baseC, rel, 'vendors.' + t + '.' + rel.join('.'), sp.path);
    };
    groups.forEach(function (g) {
      var rows = [], gate = byName[g].filter(function (sp) { return sp.widget === 'optional_group'; })[0];
      var on = !gate || vb(gate).get() !== null && vb(gate).get() !== undefined;
      byName[g].forEach(function (sp) {
        if (sp === gate) { rows.push(optionalToggle(sp, vb(sp), initialGroup(t, sp.path))); return; }
        if (gate && !on && sp.path.indexOf(gate.path + '.') === 0) return;
        rows.push(sp.widget === 'secret' ? field(sp, { path: 'vendors.' + t + '.' + sp.path.split('.').pop(), get: function () { return null; } }) : field(sp, vb(sp)));
      });
      body.appendChild(group(g, rows));
    });
  };
  // 可选分组开启时的初始值：已保存值 → 包内默认 vendor 值 → schema 缺省
  function initialGroup(t, specPath) {
    var rel = specPath.slice('vendors.*.'.length).split('.');
    return getIn(findVendor(S.base.vendors, t), rel) || getIn(findVendor(S.view.defaults.vendors, t), rel) || S.schema.groups[specPath] || {};
  }
  function optionalToggle(sp, b, initial) {
    var v = b.get(), on = v !== null && v !== undefined;
    return formRow(sp, b.path, function (id) {
      return h('input', {
        id: id, type: 'checkbox', class: 'cfg-check', checked: on, disabled: S.readonly,
        onchange: function (e) { b.set(e.target.checked ? clone(initial) : null); rerender(); },
      });
    }, [h('span', { class: 'cfg-note', text: on ? '已启用' : '未启用' })]);
  }
  function vendorHeader(t, v) {
    var chain = effectiveChain(S.work.tiers, S.work.vendors), pos = chain.indexOf(t);
    var badges = [
      v.enabled === false ? badge('Disabled', 'muted') : badge('Enabled', 'ok'),
      pos >= 0 ? badge('Tier #' + (pos + 1), 'info') : badge('Not in chain', 'warn', '未加入 tiers 链路，不参与路由'),
      (S.view.vendor_overrides || []).indexOf(t) >= 0 ? badge('Overridden', 'info', '已在 config.db 中覆盖包内默认值') : null,
    ];
    var key = 'vendors.' + t + '.api_key', st = (S.view.secrets || {})[key];
    if (st && !st.resolved && !S.ops[key]) badges.push(badge('API key unset', 'warn', 'API Key 未配置或环境变量未设置，请求将失败'));
    var scoped = (S.work.model_mapping || []).some(function (r) { return (r.vendors || []).indexOf(t) >= 0; });
    if (t !== 'anthropic' && !scoped) badges.push(badge('No scoped mapping', 'warn', '没有仅作用于该 vendor 的 model_mapping 规则，请求模型名可能被通用规则映射'));
    return h('div', { class: 'cfg-vendor-head' }, [
      sectionHead(t, null, S.readonly ? [] : [
        btn('恢复默认', function () { replaceVendor(t, clone(findVendor(S.view.defaults.vendors, t)) || { vendor: t }); }, 'cfg-btn-sm', { title: '把该 vendor 改回包内默认参数（保存后生效）' }),
        btn('移除', function () { removeVendor(t); }, 'cfg-btn-sm cfg-btn-danger', { title: '从配置中移除该 vendor（同时移出 tiers）' }),
      ]),
      h('div', { class: 'cfg-badges' }, badges),
    ]);
  }
  function addVendor(t) {
    var v = clone(findVendor(S.base.vendors, t) || findVendor(S.view.defaults.vendors, t)) || { vendor: t };
    S.work.vendors = (S.work.vendors || []).concat([v]);
    changed();
    rerender();
  }
  function replaceVendor(t, v) {
    S.work.vendors = (S.work.vendors || []).map(function (x) { return x.vendor === t ? v : x; });
    changed();
    rerender();
  }
  function removeVendor(t) {
    S.work.vendors = (S.work.vendors || []).filter(function (x) { return x.vendor !== t; });
    if (Array.isArray(S.work.tiers)) S.work.tiers = S.work.tiers.filter(function (x) { return x !== t; });
    changed();
    rerender();
  }

  RENDER.tiers = function (body) {
    var present = vendorMap(S.work.vendors), chain = effectiveChain(S.work.tiers, S.work.vendors);
    var running = (S.view.running && S.view.running.tiers) || null;
    var setChain = function (next) { S.work.tiers = next; changed(); rerender(); };
    var actions = [];
    if (!S.readonly && Array.isArray(running) && !deepEqual(running, enabledChain())) {
      actions.push(btn('使用运行中顺序', function () {
        var head = running.filter(function (t) { return present[t]; });
        setChain(head.concat(chain.filter(function (t) { return head.indexOf(t) < 0; })));
      }, 'cfg-btn-sm', { title: '采用运行实例当前的优先级（含 Overview 拖拽调整）' }));
    }
    if (!S.readonly && Array.isArray(S.work.tiers)) actions.push(btn('按 vendors 顺序', function () { setChain(null); }, 'cfg-btn-sm', { title: '不显式设置 tiers，按 vendors 列表中已启用的 vendor 顺序' }));
    actions.push(resetBtn('tiers'));
    body.appendChild(sectionHead('Tiers', spec('tiers'), actions));
    if (!Array.isArray(S.work.tiers)) body.appendChild(h('p', { class: 'cfg-note', text: '未显式设置 tiers：链路按 vendors 列表中已启用的 vendor 顺序组成。' }));
    var order = chain.concat(vendorTypes().filter(function (t) { return chain.indexOf(t) < 0; }));
    var move = function (from, to) { var c = chain.slice(), x = c.splice(from, 1)[0]; c.splice(to, 0, x); setChain(c); };
    var rows = order.map(function (t, i) {
      var inChain = i < chain.length, v = present[t], rpos = Array.isArray(running) ? running.indexOf(t) : -1;
      return h('tr', { 'data-path': 'tiers.' + t }, [
        inChain ? orderCell(chain, i, move) : h('td'),
        h('td', {}, h('input', {
          type: 'checkbox', class: 'cfg-check', checked: inChain, disabled: S.readonly || !v, 'aria-label': 'In chain: ' + t,
          onchange: function (e) { setChain(e.target.checked ? chain.concat([t]) : chain.filter(function (x) { return x !== t; })); },
        })),
        h('td', { class: 'cfg-trunc' }, h('button', { type: 'button', class: 'cfg-link', text: t, onclick: function () { goPill('vendors', t); } })),
        h('td', { class: 'cfg-trunc' }, !v ? badge('Not configured', 'muted') : v.enabled === false ? badge('Disabled', 'warn', '已禁用：运行时跳过该层级') : badge('Enabled', 'ok')),
        cell(rpos >= 0 ? '#' + (rpos + 1) : '–'),
        h('td'),
      ]);
    });
    body.appendChild(table([
      { label: 'Order', w: '--col-order' }, { label: 'In Chain', w: '--col-bool' }, { label: 'Vendor', w: '--col-vendor' },
      { label: 'Enabled', w: '--col-state' }, { label: 'Running', w: '--col-order' },
    ], rows, 'tiers'));
  };

  RENDER.failover = function (body) {
    body.appendChild(sectionHead('Failover', null, [resetBtn('failover')]));
    body.appendChild(group(null, ['status_codes', 'error_types', 'error_message_patterns'].map(function (k) {
      return field(spec('failover.' + k), bindRoot(['failover', k]));
    })));
  };

  RENDER.model_mapping = function (body) {
    body.appendChild(listBlock({
      key: 'model_mapping', parts: ['model_mapping'], specPath: 'model_mapping', title: 'Model Mapping', ordered: true,
      actions: [resetBtn('model_mapping')],
      cols: [
        { label: 'Pattern', w: '--col-pattern', mono: true, text: function (r) { return r.pattern; } },
        { label: 'Target', w: '--col-target', text: function (r) { return r.target; } },
        { label: 'Regex', w: '--col-bool', text: function (r) { return r.is_regex ? 'Yes' : 'No'; } },
        { label: 'Vendor Scope', w: '--col-list', text: function (r) { return r.vendors && r.vendors.length ? r.vendors : 'All'; } },
      ],
      fields: function (i) { return itemFields(['model_mapping'], i, ['pattern', 'target', 'is_regex', 'vendors']); },
      blank: function () { return { pattern: '', target: '', is_regex: false, vendors: [] }; },
    }));
  };

  RENDER.pricing = function (body) {
    var fresh = pricingMergeRows(S.view.overridden, S.base.pricing, S.work.pricing, S.view.defaults.pricing);
    var before = [h('p', { class: 'cfg-note', text: '单价单位为每百万 token；修改后历史用量成本按新价格重算。' })];
    if (fresh.length && !S.readonly) {
      before.push(banner('info', '包内默认定价中有 ' + fresh.length + ' 行不在已保存的定价表中（定价表已覆盖默认值，不会自动合入）',
        [btn('合并', function () { S.work.pricing = (S.work.pricing || []).concat(clone(fresh)); changed(); rerender(); }, 'cfg-btn-sm')]));
    }
    var price = function (f) { return function (r) { return r[f] === undefined ? '0' : fmtVal(r[f]); }; };
    var curSpec = { label: 'Currency', description: '整行单价的币种：$ 为 USD，¥ 为 CNY', widget: 'enum', enum: ['USD', 'CNY'] };
    body.appendChild(listBlock({
      key: 'pricing', parts: ['pricing'], specPath: 'pricing', title: 'Pricing', ordered: false, before: before,
      actions: [resetBtn('pricing')],
      cols: [
        { label: 'Vendor', w: '--col-vendor', text: function (r) { return r.vendor; } },
        { label: 'Model', w: '--col-model', mono: true, text: function (r) { return r.model; } },
        { label: 'Currency', w: '--col-currency', text: entryCurrency },
        { label: 'Input', w: '--col-price', mono: true, text: price(PRICE_FIELDS[0]) },
        { label: 'Output', w: '--col-price', mono: true, text: price(PRICE_FIELDS[1]) },
        { label: 'Cache W', w: '--col-price', mono: true, text: price(PRICE_FIELDS[2]) },
        { label: 'Cache R', w: '--col-price', mono: true, text: price(PRICE_FIELDS[3]) },
      ],
      fields: function (i) {
        var row = function () { return S.work.pricing[i]; };
        var cur = {
          path: 'pricing.' + i + '.currency', optLabel: function (c) { return c === 'CNY' ? 'CNY ¥' : 'USD $'; },
          get: function () { return entryCurrency(row()); },
          set: function (c) {
            var next = withCurrency(row(), c), orig = (S.base.pricing || [])[i];
            // 改回原币种且金额未变 → 恢复原始写法，dirty 随之清除
            if (orig && entryCurrency(orig) === c && deepEqual(withCurrency(orig, c), next)) next = clone(orig);
            S.work.pricing[i] = next;
            changed();
          },
        };
        return itemFields(['pricing'], i, ['vendor', 'model']).concat([{ spec: curSpec, bind: cur }], PRICE_FIELDS.map(function (f) {
          return {
            spec: spec('pricing.*.' + f),
            bind: {
              path: 'pricing.' + i + '.' + f, raw: true,
              get: function () { var e = row(); return e && e[f] !== undefined ? parsePrice(e[f]).amount : '0'; },
              set: function (v) { var e = row(); e[f] = priceText(entryCurrency(e), v === '' ? '0' : v); changed(); },
            },
          };
        }));
      },
      blank: function () { return { vendor: '', model: '', input_cost_per_mtok: '$0.0', output_cost_per_mtok: '$0.0', cache_write_cost_per_mtok: '$0.0', cache_read_cost_per_mtok: '$0.0' }; },
    }));
  };

  RENDER.native_api = function (body) {
    body.appendChild(sectionHead('Native API', null, [resetBtn('native_api')]));
    var byGroup = {}, order = [];
    S.specs.forEach(function (sp) {
      if (sp.path.indexOf('native_api.') !== 0) return;
      var g = sp.path.split('.')[1];
      if (!byGroup[g]) { byGroup[g] = []; order.push(g); }
      byGroup[g].push(field(sp, bindRoot(sp.path.split('.'))));
    });
    order.forEach(function (g) { body.appendChild(group(g, byGroup[g])); });
  };

  RENDER.antigravity_gateway = function (body) {
    body.appendChild(sectionHead('Antigravity Gateway', null, [resetBtn('antigravity_gateway')]));
    var fields = [];
    S.specs.forEach(function (sp) {
      if (sp.path.indexOf('antigravity_gateway.') !== 0) return;
      fields.push(field(sp, bindRoot(sp.path.split('.'))));
    });
    body.appendChild(group('antigravity', fields));
  };

  RENDER.session_policies = function (body) {
    var parts = ['session_policies'];
    body.appendChild(sectionHead('Session Policies', null, [resetBtn('session_policies')]));
    body.appendChild(listBlock({
      key: 'policies', parts: parts.concat(['policies']), specPath: 'session_policies.policies', title: 'Policies', ordered: true,
      cols: [
        { label: 'Name', w: '--col-name', text: function (r) { return r.name; } },
        { label: 'Session Keys', w: '--col-list', mono: true, text: function (r) { return (r.match && r.match.session_keys) || []; } },
        { label: 'Tiers', w: '--col-list', text: function (r) { return r.tiers || []; } },
        { label: 'Model', w: '--col-model', mono: true, text: function (r) { return r.model || '–'; } },
      ],
      fields: function (i) { return itemFields(parts.concat(['policies']), i, ['name', 'match.session_keys', 'tiers', 'model']); },
      blank: function () { return { name: '', match: { session_keys: [] }, tiers: [], model: null }; },
    }));
    body.appendChild(listBlock({
      key: 'bindable', parts: parts.concat(['bindable_models']), specPath: 'session_policies.bindable_models',
      title: 'Bindable Models', ordered: true,
      cols: [
        { label: 'Label', w: '--col-name', text: function (r) { return r.label; } },
        { label: 'Model', w: '--col-model', mono: true, text: function (r) { return r.model; } },
        { label: 'Tiers', w: '--col-list', text: function (r) { return r.tiers || []; } },
        { label: 'Excl', w: '--col-bool', text: function (r) { return r.exclude ? 'Yes' : 'No'; } },
      ],
      fields: function (i) { return itemFields(parts.concat(['bindable_models']), i, ['label', 'model', 'tiers', 'exclude']); },
      blank: function () { return { label: '', model: '', tiers: [], exclude: false }; },
    }));
    body.appendChild(listBlock({
      key: 'bindings', parts: parts.concat(['title_vendor_bindings']), specPath: 'session_policies.title_vendor_bindings',
      title: 'Title Bindings', ordered: true,
      cols: [
        { label: 'Prefix', w: '--col-pattern', mono: true, text: function (r) { return r.prefix; } },
        { label: 'Vendor', w: '--col-vendor', text: function (r) { return r.vendor; } },
      ],
      fields: function (i) { return itemFields(parts.concat(['title_vendor_bindings']), i, ['prefix', 'vendor']); },
      blank: function () { return { prefix: '', vendor: '' }; },
    }));
    body.appendChild(group(null, [field(spec('session_policies.title_exempt_prefixes'), bindRoot(['session_policies', 'title_exempt_prefixes']))]));
  };

  RENDER.system = function (body) {
    var v = S.view;
    body.appendChild(sectionHead('System', null, []));
    body.appendChild(h('p', { class: 'cfg-note', text: 'Bootstrap 字段只读：修改请运行 coding-proxy config set <path> <value>，重启后生效。Google Secret 例外：可在此更改，仅用于 CLI 登录流程（auth login、start 自动登录），不影响运行中的代理。' }));
    var groups = [], byGroup = {};
    S.specs.forEach(function (sp) {
      var section = sp.path.split('.')[0];
      if (['server', 'logging', 'database', 'auth'].indexOf(section) < 0) return;
      var g = sp.group || section;
      if (!byGroup[g]) { byGroup[g] = []; groups.push(g); }
      if (sp.widget === 'secret') { byGroup[g].push(field(sp, { path: sp.path, get: function () { return null; } })); return; }
      var value = getIn(v.values, sp.path.split('.'));
      var src = (v.sources || {})[sp.path] || ((v.overridden || []).indexOf(section) >= 0 ? 'store' : 'default');
      byGroup[g].push(formRow(sp, sp.path, function (id) { return h('span', { id: id, class: 'cfg-ro cfg-trunc', text: fmtVal(value) }); },
        [badge(src.toUpperCase(), src === 'default' ? 'muted' : 'info', '值来源：' + src)]));
    });
    groups.forEach(function (g) { body.appendChild(group(g, byGroup[g])); });
    var info = function (label, desc, kids) {
      return formRow({ label: label, description: desc }, null, function () { return h('div', { class: 'cfg-ro-list' }, kids); });
    };
    var list = function (items, empty) {
      return items.length ? items.map(function (t) { return h('div', { class: 'cfg-trunc', text: t }); }) : [h('span', { class: 'cfg-note', text: empty })];
    };
    body.appendChild(group('Migration', [
      info('Mode', '配置来源模式', [badge(v.mode, v.mode === 'store' ? 'ok' : v.mode === 'defaults' ? 'info' : 'err'), ' ', MODE_TEXT[v.mode] || '']),
      info('Apply', '保存后的生效方式', [v.apply_supported ? '保存即在线生效（generation swap）' : '保存后需重启生效']),
      info('Overridden', '已在 config.db 中覆盖默认值的 section', list(v.overridden || [], '无（全部为包内默认值）')),
      info('Notices', '启动与迁移提示（如旧 YAML 的处理建议）', list(v.notices || [], '无')),
      info('Errors', '启动或加载时的配置错误', list((v.errors || []).map(function (e) { return (locToPath(e.loc) || '<root>') + '：' + e.msg; }), '无')),
    ]));
  };

  // ── Dirty / 错误 / 保存 ───────────────────────────────────
  function isDirty() { return !!S.view && (dirtySections(S.base, S.work).length > 0 || activeOps(S.ops).length > 0); }
  function changed() { updateDirty(); }
  function affectedSections() {
    var set = {};
    dirtySections(S.base, S.work).forEach(function (s) { set[s] = true; });
    activeOps(S.ops).forEach(function (p) { set[p.split('.')[0]] = true; });
    return Object.keys(set);
  }
  function updateDirty() {
    var dirty = isDirty(), tab = document.getElementById('tab-btn-config');
    if (tab) tab.classList.toggle('cfg-dirty', dirty);
    if (!S.root || !S.view) return;
    // 保存进行中（含确认框与重载）冻结表单：期间的编辑会被随后的整体重载覆盖
    var body = S.root.querySelector('.cfg-body');
    if (body) {
      ['inert', 'aria-busy'].forEach(function (a) { if (S.saving) body.setAttribute(a, a === 'inert' ? '' : 'true'); else body.removeAttribute(a); });
    }
    Array.prototype.forEach.call(S.root.querySelectorAll('.cfg-toolbar .cfg-btn'), function (b) { b.disabled = S.saving; });
    var pills = {};
    affectedSections().forEach(function (s) { pills[pillOf(s)] = true; });
    var errs = {};
    S.errors.forEach(function (e) { errs[pillOf(locToPath(e.loc))] = true; });
    Array.prototype.forEach.call(S.root.querySelectorAll('.cfg-pills .range-btn'), function (b) {
      b.classList.toggle('cfg-has-dot', !!pills[b.getAttribute('data-pill')]);
      b.classList.toggle('cfg-has-err', !!errs[b.getAttribute('data-pill')]);
    });
    Array.prototype.forEach.call(S.root.querySelectorAll('.cfg-subpills .range-btn'), function (b) {
      b.classList.toggle('cfg-has-dot', vendorDirty(b.getAttribute('data-vendor')));
    });
    var bar = S.root.querySelector('.cfg-savebar');
    if (bar) fillSaveBar(bar);
  }
  function renderSaveBar() { var bar = h('div', { class: 'cfg-savebar', role: 'region', 'aria-label': 'Save changes' }); fillSaveBar(bar); return bar; }
  function fillSaveBar(bar) {
    var dirty = isDirty(), n = countChangedFields(S.base, S.work, S.ops), k = affectedSections().length;
    bar.hidden = !dirty && !S.errors.length;
    clear(bar);
    var msg = h('div', { class: 'cfg-savebar-msg' }, [h('div', { text: dirty ? n + ' 个字段待保存（' + k + ' 个 section）' : '没有未保存的修改' })]);
    if (S.errors.length) {
      msg.appendChild(h('ul', { class: 'cfg-savebar-errors' }, S.errors.slice(0, 8).map(function (e) {
        var p = locToPath(e.loc);
        return h('li', { class: 'cfg-trunc' }, h('button', { type: 'button', class: 'cfg-link', text: (p || '<root>') + '：' + e.msg, onclick: function () { focusError(p); } }));
      }).concat(S.errors.length > 8 ? [h('li', { text: '…共 ' + S.errors.length + ' 个错误' })] : [])));
    }
    add(bar, [msg, btn('放弃修改', discard, '', { disabled: !dirty || S.saving }),
      btn(S.saving ? '保存中…' : '保存 ' + n + ' 个字段（' + k + ' 个 section）', onSave, 'cfg-btn-primary', { disabled: !dirty || S.saving || S.readonly })]);
  }
  function discard() {
    if (!window.confirm('放弃全部未保存的修改？')) return;
    EDITABLE.forEach(function (s) { S.work[s] = clone(S.base[s]); });
    S.ops = {};
    S.errors = [];
    rerender();
  }
  function focusError(path) {
    S.pill = pillOf(path);
    var parts = path.split('.');
    if (parts[0] === 'vendors' && parts[1]) S.vendor = parts[1];
    if (/^\d+$/.test(parts[1])) S.expanded[parts[0]] = Number(parts[1]);
    if (parts[0] === 'session_policies' && /^\d+$/.test(parts[2])) S.expanded[parts[1] === 'policies' ? 'policies' : 'bindings'] = Number(parts[2]);
    rerender();
    var el = S.root.querySelector('.cfg-invalid');
    if (el && el.scrollIntoView) el.scrollIntoView({ block: 'center' });
  }
  function applyErrors() {
    var nodes = {};
    Array.prototype.forEach.call(S.root.querySelectorAll('[data-path]'), function (el) { nodes[el.getAttribute('data-path')] = el; });
    S.errors.forEach(function (e) {
      var path = locToPath(e.loc), cands = pathCandidates(path);
      for (var i = 0; i < cands.length; i++) {
        var el = nodes[cands[i]];
        if (!el) continue;
        el.classList.add('cfg-invalid');
        // 表单行：错误写在控件下方；表格行仅标红；容器（section / 表格）：带路径置顶
        var card = el.tagName === 'TR' && el.nextElementSibling && el.nextElementSibling.querySelector('.cfg-detail');
        if (el.classList.contains('cfg-row')) el.querySelector('.cfg-control').appendChild(h('div', { class: 'cfg-err', text: e.msg }));
        else if (card) card.insertBefore(h('div', { class: 'cfg-err', text: e.msg }), card.firstChild);
        else if (el.tagName !== 'TR') el.insertBefore(h('div', { class: 'cfg-err', text: path + '：' + e.msg }), el.firstChild);
        return;
      }
    });
  }
  function fetchStatus() {
    return fetch('/api/status', { cache: 'no-store' }).then(function (r) { return r.ok ? r.json() : null; }).catch(function () { return null; });
  }
  function runtimeLines(status) {
    if (!S.view.apply_supported) return ['当前实例不支持在线生效：保存后需重启，运行时状态不受影响。'];
    var tiers = (status && status.tiers) || [], lines = [];
    var names = function (pred) { return tiers.filter(pred).map(function (t) { return t.name; }); };
    var cb = names(function (t) { return t.circuit_breaker && t.circuit_breaker.state && t.circuit_breaker.state !== 'closed'; });
    var rl = names(function (t) { return t.rate_limit && t.rate_limit.is_rate_limited; });
    var qx = names(function (t) { return [t.quota_guard, t.weekly_quota_guard].some(function (q) { return q && q.state === 'quota_exceeded'; }); });
    if (status && typeof status.in_flight === 'number') lines.push('进行中的请求：' + status.in_flight + ' 个（在旧实例上继续完成）');
    if (cb.length) lines.push('熔断状态将重置：' + cb.join('、'));
    if (rl.length) lines.push('限流状态将重置：' + rl.join('、'));
    if (qx.length) lines.push('配额超限状态按用量数据库重建：' + qx.join('、'));
    lines.push('运行时调整的并发上限恢复为配置值；配额窗口按用量数据库重建。');
    var running = S.view.running && S.view.running.tiers;
    if (Array.isArray(running) && !deepEqual(running, enabledChain())) {
      lines.push('运行中的优先级（含 Overview 拖拽）与保存的 tiers 不同，保存后以 tiers 为准。');
    }
    return lines;
  }
  function confirmSave(payload, status) {
    var secretFields = {};
    S.specs.forEach(function (sp) {
      if (sp.widget !== 'secret' || !sp.vendors) return;
      sp.vendors.forEach(function (t) { (secretFields[t] = secretFields[t] || []).push(sp.path.split('.').pop()); });
    });
    var cleared = predictClearedSecrets({
      base: S.base, work: S.work, defaults: S.view.defaults, secrets: S.view.secrets, ops: S.ops, secretFields: secretFields,
      targetDefaults: { base_url: dflt('vendors.*.base_url'), token_url: dflt('vendors.*.token_url') },
    });
    var blocks = [
      ['修改的 section', Object.keys(payload.sections)],
      ['密钥操作', Object.keys(payload.secret_ops).map(function (p) { return p + '：' + payload.secret_ops[p].op; })],
      ['目标地址已变更，以下密钥将被清空（需重新设置）', cleared],
      ['运行时影响', runtimeLines(status)],
    ].filter(function (b) { return b[1].length; });
    var text = blocks.map(function (b) { return b[0] + '：\n- ' + b[1].join('\n- '); }).join('\n\n');
    if (typeof window.HTMLDialogElement !== 'function') return Promise.resolve(window.confirm('确认保存？\n\n' + text));
    return new Promise(function (resolve) {
      var dlg = h('dialog', { class: 'cfg-dialog', 'aria-label': '确认保存' });
      var close = function (ok) { dlg.close(); dlg.remove(); resolve(ok); };
      add(dlg, [h('div', { class: 'cfg-section-title', text: '确认保存' })].concat(blocks.map(function (b, i) {
        return h('div', { class: 'cfg-dialog-block' + (b[1] === cleared ? ' cfg-dialog-warn' : '') }, [
          h('div', { class: 'cfg-group-title', text: b[0] }),
          h('ul', {}, b[1].map(function (l) { return h('li', { text: l }); })),
        ]);
      }), [h('div', { class: 'cfg-actions' }, [btn('取消', function () { close(false); }), btn('确认保存', function () { close(true); }, 'cfg-btn-primary')])]));
      dlg.addEventListener('cancel', function (e) { e.preventDefault(); close(false); });
      document.body.appendChild(dlg);
      dlg.showModal();
    });
  }
  function onSave() {
    if (S.saving || S.readonly || !isDirty()) return;
    var payload = buildPayload(S.view.revisions, S.base, S.work, S.ops);
    var empty = Object.keys(payload.secret_ops).filter(function (p) { var o = payload.secret_ops[p]; return o.op === 'set' && !o.value.trim(); });
    if (empty.length) {
      S.errors = empty.map(function (p) { return { loc: ['secret_ops', p], msg: '请输入新值，或选择「撤销更改」' }; });
      rerender();
      return;
    }
    S.saving = true;
    updateDirty();
    fetchStatus().then(function (status) { return confirmSave(payload, status); }).then(function (ok) {
      if (!ok) { S.saving = false; updateDirty(); return null; }
      return request('PUT', payload).then(function (r) { return handleSave(r, payload); });
    }).catch(function () {
      S.saving = false;
      S.notice = { kind: 'err', text: '保存请求失败：网络错误' };
      rerender();
    });
  }
  // PUT 成功即在本地落定：已提交 section 并入 base，丢弃密钥明文；随后重载失败也不残留 dirty / 明文
  function commitSaved(payload, revisions) {
    Object.keys(payload.sections).forEach(function (s) { S.base[s] = clone(payload.sections[s]); S.work[s] = clone(payload.sections[s]); });
    [payload.secret_ops, S.ops].forEach(function (ops) { Object.keys(ops).forEach(function (p) { if (ops[p]) delete ops[p].value; }); });
    S.ops = {};
    S.errors = [];
    if (isObj(revisions)) Object.keys(revisions).forEach(function (s) { S.view.revisions[s] = revisions[s]; });
  }
  // 重载完成前保持冻结：重载会覆盖工作副本；'locked'（已渲染解锁界面）不叠 fatal 视图
  function reloadAfterSave(only) {
    return load(only).then(function (ok) {
      S.saving = false;
      updateDirty();
      if (ok === false && only) conflictReloadFailed(only);
    });
  }

  // 409 冲突 section 定向重载失败：不得保留「已重新加载 / 其余保留」的承诺；
  // 重试按钮限定为仅重载冲突 section（保住其余 section 的本地修改）
  function conflictReloadFailed(secs) {
    S.notice = {
      kind: 'err',
      text: '以下 section 已被其他会话或 CLI 修改，但重新加载失败：' + secs.join(', ') +
        '。其中的本地修改已丢弃，其余修改仍保留在本地；请点击「重试」仅重新加载上述 section。',
    };
    S.retryOnly = secs;
    renderFatal(S.fatalText);
  }
  function handleSave(r, payload) {
    var d = r.data;
    if (r.status === 200) {
      commitSaved(payload, d.revisions);
      var lines = [];
      if ((d.cleared_secrets || []).length) lines.push('目标地址变更，已清空密钥：' + d.cleared_secrets.join('、'));
      if ((d.reset_runtime || []).length) lines.push('已重置的运行时状态：' + d.reset_runtime.join('；'));
      // auth.google_client_secret 只被 CLI 登录流程读取，运行中的代理不受其影响
      var cliOps = Object.keys(payload.secret_ops).filter(function (p) { return pillOf(p) === 'system'; });
      var cliOnly = cliOps.length > 0 && cliOps.length === Object.keys(payload.secret_ops).length && !Object.keys(payload.sections).length;
      if (cliOps.length) lines.push('Google Secret 仅用于 CLI 登录流程（auth login、start 自动登录），不影响运行中的代理');
      S.notice = d.applied
        ? { kind: 'ok', text: cliOnly ? '已保存' : '已保存并生效', lines: lines }
        : { kind: 'warn', text: '已保存，尚未生效：' + (d.apply_error === 'restart required' ? '需重启 coding-proxy' : d.apply_error || '未知原因'), lines: lines };
      return reloadAfterSave();
    }
    if (r.status === 409) {
      var secs = d.sections || [];
      S.errors = [];
      S.notice = { kind: 'warn', text: '以下 section 已被其他会话或 CLI 修改，已重新加载（其中的本地修改已丢弃）：' + secs.join(', ') + '。其余修改保留，请确认后重新保存。' };
      return reloadAfterSave(secs);
    }
    S.saving = false;
    if (r.status === 422) {
      S.errors = (d.errors || []).slice();
      if (S.errors.length) focusError(locToPath(S.errors[0].loc));
      else rerender();
      return null;
    }
    if (r.status === 401) { forgetToken(); renderLocked('管理 token 无效或已更换，请重新解锁（未保存的修改已丢失）'); return null; }
    if (r.status === 423) { S.readonly = true; S.notice = { kind: 'err', text: errText(d) || MODE_TEXT.legacy_readonly }; }
    else S.notice = { kind: 'err', text: '保存失败（HTTP ' + r.status + '）' + (errText(d) ? '：' + errText(d) : '') };
    rerender();
    return null;
  }

  // ── 入口 ──────────────────────────────────────────────────
  // 截断时补 title（Config 与 Sessions 表格共用；仅 scrollWidth 溢出时出现）
  function truncTitle(e) {
    var el = e.target && e.target.closest && e.target.closest('.cfg-trunc, .session-table th, .session-table tr[data-row] > td:not(.cell-bind)');
    if (!el || (el.title && !el.getAttribute('data-auto-title'))) return; // 显式 title 优先
    if (el.scrollWidth > el.clientWidth) { el.title = el.textContent; el.setAttribute('data-auto-title', '1'); }
    else if (el.getAttribute('data-auto-title')) { el.removeAttribute('title'); el.removeAttribute('data-auto-title'); }
  }
  function readSchemaDefaults() {
    var el = document.getElementById('config-schema-defaults');
    try { return el ? JSON.parse(el.textContent) : { fields: {}, groups: {} }; } catch (e) { return { fields: {}, groups: {} }; }
  }
  function mount(el) {
    if (!el || S.root === el) return;
    S.root = el;
    S.schema = readSchemaDefaults();
    readUrlSection();
    clear(el);
    el.appendChild(h('div', { class: 'card empty', text: '切换到 Config 页时加载配置…' }));
  }
  function refresh(force) {
    if (!S.root) mount(document.getElementById('config-root'));
    if (S.saving) return Promise.resolve(); // 保存结束后自行重载
    if (!force && isDirty() && !window.confirm('放弃未保存的修改并重新加载？')) return Promise.resolve();
    return load();
  }

  captureFragmentToken();
  document.addEventListener('mouseover', truncTitle);
  document.addEventListener('focusin', truncTitle);
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape' || !S.root) return;
    Array.prototype.forEach.call(S.root.querySelectorAll('.cfg-info-wrap'), function (w) { w.classList.add('cfg-tip-off'); });
  });
  window.addEventListener('beforeunload', function (e) { if (isDirty()) { e.preventDefault(); e.returnValue = ''; } });
  mount(document.getElementById('config-root'));
  window.ConfigTab = { mount: mount, refresh: refresh, isDirty: isDirty };
})();
