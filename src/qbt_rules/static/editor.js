/*
 * qbt-rules :: rules.yml editor (/rules/edit)
 *
 * Plain script, no build step: Ace (vendored under static/vendor/ace) is
 * the text editor; everything else is this file. The server is the source
 * of truth for what's valid -- every edit is posted to /api/rules/validate
 * (debounced) and the issues it returns are drawn as gutter annotations,
 * squiggles, the Problems list and the outline. Saving PUTs the whole file
 * with If-Match set to the ETag it was loaded with, so a save can never
 * silently overwrite a change made elsewhere (412 -> conflict dialog).
 *
 * Anything that came out of rules.yml/config.yml or the server is rendered
 * with textContent, never innerHTML.
 */
(function () {
  'use strict';

  var CFG = window.QBT_EDITOR;
  var $ = function (id) { return document.getElementById(id); };
  var Range = ace.require('ace/range').Range;
  var langTools = ace.require('ace/ext/language_tools');
  ace.config.set('basePath', CFG.urls.acePath);

  var DRAFT_KEY = 'qbt-rules:editor-draft';
  var REVIEW_KEY = 'qbt-rules:editor-review';

  var state = {
    tab: 'rules',
    baseline: null,          // {content, etag, modified_at} as last loaded/saved
    path: '',
    editable: false,
    readonlyReason: null,
    validation: null,        // last /api/rules/validate response for the buffer
    summary: null,           // last summary that parsed (kept through syntax errors)
    validationSeq: 0,
    validationPending: false,
    saving: false,
    catalog: null,
    config: null,            // /api/config/source response
    ruleHits: null,          // by_rule from the last dry run
    historySelected: null,
    conflict: null,
    loading: true,           // ignore buffer changes made while loading
    markers: []
  };

  // ---------------------------------------------------------------------
  // Helpers
  // ---------------------------------------------------------------------

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function storage(fn, fallback) {
    try { return fn(window.localStorage); } catch (e) { return fallback; }
  }

  var toastTimer = null;
  function toast(message, kind) {
    var t = $('toast');
    t.textContent = message;
    t.className = 'ed-toast' + (kind ? ' ' + kind : '');
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(function () { t.hidden = true; }, kind === 'err' ? 7000 : 3500);
  }

  function api(url, opts) {
    opts = opts || {};
    var headers = Object.assign({ 'Accept': 'application/json' }, opts.headers || {});
    var method = opts.method || 'GET';
    if (method !== 'GET') headers['X-CSRF-Token'] = CFG.csrfToken;
    if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
    return fetch(url, {
      method: method,
      headers: headers,
      credentials: 'same-origin',
      body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        if (res.status === 401) {
          toast('Your session has expired — reload the page to log in again.', 'err');
        }
        return { status: res.status, ok: res.ok, data: data || {} };
      });
    }, function () {
      return { status: 0, ok: false, data: { message: 'Could not reach the server' } };
    });
  }

  function when(iso) {
    if (!iso) return '';
    var d = new Date(iso);
    return isNaN(d) ? iso : d.toLocaleString();
  }

  function plural(n, word) { return n + ' ' + word + (n === 1 ? '' : 's'); }

  // ---------------------------------------------------------------------
  // Editor
  // ---------------------------------------------------------------------

  var editor = ace.edit('ace-host', {
    mode: 'ace/mode/yaml',
    tabSize: 2,
    useSoftTabs: true,
    showPrintMargin: false,
    displayIndentGuides: true,
    scrollPastEnd: 0.4,
    enableBasicAutocompletion: true,
    enableLiveAutocompletion: true,
    enableSnippets: false,
    useWorker: false
  });

  function newSession() {
    var s = ace.createEditSession('', 'ace/mode/yaml');
    s.setUseWorker(false);
    s.setTabSize(2);
    s.setUseSoftTabs(true);
    s.setUndoManager(new ace.UndoManager());
    return s;
  }
  var rulesSession = newSession();
  var configSession = newSession();
  editor.setSession(rulesSession);

  var lightScheme = window.matchMedia('(prefers-color-scheme: light)');
  function applyTheme() {
    editor.setTheme(lightScheme.matches ? 'ace/theme/tomorrow' : 'ace/theme/tomorrow_night');
  }
  applyTheme();
  if (lightScheme.addEventListener) lightScheme.addEventListener('change', applyTheme);

  function isDirty() {
    return !!state.baseline && rulesSession.getValue() !== state.baseline.content;
  }

  // Replace the buffer through the document rather than session.setValue(),
  // so Ctrl+Z can step back to what was there before
  function replaceBuffer(text) {
    rulesSession.getDocument().setValue(text);
  }

  // ---------------------------------------------------------------------
  // Status / toolbar
  // ---------------------------------------------------------------------

  function updateState() {
    var dot = $('state-dot');
    var label = $('state-label');
    var dirty = isDirty();
    var counts = state.validation ? state.validation.counts : null;
    var hasErrors = !!(counts && counts.error);

    dot.className = 'ed-dot';
    if (state.tab === 'config') {
      label.textContent = 'Read-only';
    } else if (!state.baseline) {
      dot.className += ' busy';
      label.textContent = 'Loading…';
    } else if (state.saving) {
      dot.className += ' busy';
      label.textContent = 'Saving…';
    } else if (dirty) {
      dot.className += state.validationPending ? ' busy' : ' dirty';
      label.textContent = state.validationPending ? 'Checking…' : 'Unsaved changes';
    } else {
      label.textContent = state.editable ? 'Saved' : 'Read-only';
    }

    var issues = currentIssues();
    ['error', 'warning', 'info'].forEach(function (sev) {
      var n = issues.filter(function (i) { return i.severity === sev; }).length;
      var badge = $('count-' + sev);
      badge.hidden = n === 0;
      badge.textContent = n + ' ' + (sev === 'error' ? (n === 1 ? 'error' : 'errors')
        : sev === 'warning' ? (n === 1 ? 'warning' : 'warnings') : 'info');
    });

    var onRules = state.tab === 'rules';
    var save = $('btn-save');
    save.disabled = !onRules || !state.editable || !dirty || state.saving || hasErrors;
    save.title = !state.editable ? (state.readonlyReason || 'Read-only')
      : hasErrors ? 'Fix the errors first' : !dirty ? 'No changes' : 'Save rules.yml';
    $('btn-diff').disabled = !onRules || !state.baseline;
    $('btn-history').disabled = !onRules;
    $('btn-dryrun').disabled = !onRules;
    editor.setReadOnly(!onRules || !state.editable);
  }

  // ---------------------------------------------------------------------
  // Loading
  // ---------------------------------------------------------------------

  function loadSource() {
    return api(CFG.urls.source).then(function (r) {
      if (!r.ok) {
        toast(r.data.message || 'Could not load rules.yml', 'err');
        return null;
      }
      state.baseline = { content: r.data.content, etag: r.data.etag, modified_at: r.data.modified_at };
      state.path = r.data.path;
      state.editable = !!r.data.editable;
      state.readonlyReason = r.data.readonly_reason;
      showReadonlyBanner();
      updateStatusbar();
      return r.data;
    });
  }

  function showReadonlyBanner() {
    var b = $('banner-readonly');
    b.hidden = state.editable || state.tab !== 'rules';
    b.textContent = state.readonlyReason ? 'Read-only: ' + state.readonlyReason + '.' : '';
  }

  function updateStatusbar() {
    if (state.tab === 'config' && state.config) {
      $('sb-path').textContent = state.config.path;
      $('sb-modified').textContent = '';
    } else {
      $('sb-path').textContent = state.path;
      $('sb-modified').textContent = state.baseline ? 'Saved ' + when(state.baseline.modified_at) : '';
    }
  }

  // ---------------------------------------------------------------------
  // Validation + diagnostics
  // ---------------------------------------------------------------------

  var validateTimer = null;
  function scheduleValidate(delay) {
    state.validationPending = true;
    clearTimeout(validateTimer);
    validateTimer = setTimeout(runValidate, delay === undefined ? 450 : delay);
    updateState();
  }

  function runValidate() {
    clearTimeout(validateTimer);
    var seq = ++state.validationSeq;
    var content = rulesSession.getValue();
    state.validationPending = true;
    return api(CFG.urls.validate, { method: 'POST', body: { content: content } }).then(function (r) {
      if (seq !== state.validationSeq) return;  // a newer edit is already being checked
      state.validationPending = false;
      if (!r.ok) {
        updateState();
        if (r.status !== 401) toast(r.data.message || 'Validation failed', 'err');
        return;
      }
      state.validation = r.data;
      if (r.data.summary && (r.data.summary.rules.length || !hasSyntaxError(r.data))) {
        state.summary = r.data.summary;
      }
      renderDiagnostics();
      renderOutline();
      updateState();
    });
  }

  // Wait for the buffer's current content to have been validated
  function flushValidation() {
    if (!state.validationPending) return Promise.resolve();
    return runValidate();
  }

  function hasSyntaxError(v) {
    return v.issues.some(function (i) { return /^YAML syntax error/.test(i.message); });
  }

  function currentIssues() {
    if (state.tab === 'config') return state.config ? state.config.issues : [];
    return state.validation ? state.validation.issues : [];
  }

  function aceType(sev) { return sev === 'error' ? 'error' : sev === 'warning' ? 'warning' : 'info'; }

  function renderDiagnostics() {
    var session = state.tab === 'config' ? configSession : rulesSession;
    var issues = currentIssues();

    session.setAnnotations(issues.filter(function (i) { return i.line; }).map(function (i) {
      return { row: i.line - 1, column: (i.column || 1) - 1, text: i.message, type: aceType(i.severity) };
    }));

    state.markers.forEach(function (m) { m.session.removeMarker(m.id); });
    state.markers = [];
    issues.forEach(function (i) {
      if (!i.line) return;
      var row = i.line - 1;
      var startCol = (i.column || 1) - 1;
      var endRow = row;
      var endCol;
      if (i.end_line === i.line && i.end_column) {
        endCol = i.end_column - 1;
      } else {
        endCol = session.getLine(row).length;
      }
      if (endCol <= startCol) endCol = session.getLine(row).length;
      var id = session.addMarker(new Range(row, startCol, endRow, endCol), 'ed-marker-' + i.severity, 'text', true);
      state.markers.push({ session: session, id: id });
    });

    renderProblems();
  }

  var SEVERITY_RANK = { error: 0, warning: 1, info: 2 };
  var SEVERITY_GLYPH = { error: '✕', warning: '!', info: 'i' };

  function renderProblems() {
    var list = $('problem-list');
    list.textContent = '';
    var issues = currentIssues().slice().sort(function (a, b) {
      return (SEVERITY_RANK[a.severity] - SEVERITY_RANK[b.severity]) || ((a.line || 0) - (b.line || 0));
    });
    $('problems-summary').textContent = issues.length ? '(' + issues.length + ')' : '';
    if (!issues.length) {
      list.appendChild(el('div', 'ed-problem-empty',
        state.tab === 'config' ? 'Nothing to report.' : 'No problems — every rule loads and resolves.'));
      return;
    }
    issues.forEach(function (i) {
      var row = el('div', 'ed-problem');
      row.setAttribute('role', 'button');
      row.tabIndex = 0;
      row.appendChild(el('span', 'sev ' + i.severity, SEVERITY_GLYPH[i.severity]));
      var msg = el('span', 'msg', i.message);
      if (i.path) msg.appendChild(el('span', 'path', i.path));
      row.appendChild(msg);
      row.appendChild(el('span', 'where', i.line ? 'Ln ' + i.line : ''));
      var go = function () { if (i.line) gotoLine(i.line, i.column); };
      row.addEventListener('click', go);
      row.addEventListener('keydown', function (e) { if (e.key === 'Enter') go(); });
      list.appendChild(row);
    });
  }

  function gotoLine(line, column) {
    editor.gotoLine(line, Math.max(0, (column || 1) - 1), true);
    editor.scrollToLine(line - 1, true, true);
    editor.focus();
    var session = editor.getSession();
    var id = session.addMarker(new Range(line - 1, 0, line - 1, 1), 'ed-flash', 'fullLine');
    setTimeout(function () { session.removeMarker(id); }, 900);
  }

  // ---------------------------------------------------------------------
  // Outline
  // ---------------------------------------------------------------------

  function issueCountsByRule() {
    var counts = {};
    (state.validation ? state.validation.issues : []).forEach(function (i) {
      var m = /^rules\[(\d+)\]/.exec(i.path || '');
      if (!m) return;
      var c = counts[m[1]] || (counts[m[1]] = { error: 0, warning: 0 });
      if (i.severity in c) c[i.severity] += 1;
    });
    return counts;
  }

  function renderOutline() {
    var root = $('outline');
    root.textContent = '';
    var summary = state.summary;
    if (state.tab === 'config') {
      root.appendChild(el('div', 'ed-problem-empty', 'No outline for config.yml.'));
      return;
    }
    if (!summary) return;
    var filter = $('outline-filter').value.trim().toLowerCase();
    var match = function (s) { return !filter || String(s || '').toLowerCase().indexOf(filter) !== -1; };
    var counts = issueCountsByRule();

    var rules = summary.rules.filter(function (r) { return match(r.id) || match(r.description); });
    var section = el('div', 'ed-section');
    var h = el('h3');
    h.appendChild(el('span', null, 'Rules'));
    h.appendChild(el('span', null, filter ? rules.length + ' / ' + summary.rules.length : summary.rules.length));
    section.appendChild(h);
    rules.forEach(function (r) {
      var item = el('div', 'ed-item' + (r.enabled ? '' : ' disabled'));
      item.dataset.line = r.line || '';
      item.appendChild(el('span', 'idx', r.index + 1));
      var main = el('span', 'main');
      main.appendChild(el('span', 'name', r.id));
      if (r.description) main.appendChild(el('span', 'desc', r.description));
      item.appendChild(main);
      var c = counts[r.index];
      if (c && c.error) item.appendChild(el('span', 'tag err', c.error));
      else if (c && c.warning) item.appendChild(el('span', 'tag warn', c.warning));
      if (state.ruleHits && state.ruleHits[r.id] && state.ruleHits[r.id].matched) {
        var hit = el('span', 'tag hit', state.ruleHits[r.id].matched);
        hit.title = 'Matched ' + plural(state.ruleHits[r.id].matched, 'torrent') + ' in the last dry run';
        item.appendChild(hit);
      }
      if (r.stop_on_match) {
        var stop = el('span', 'tag stop', 'stop');
        stop.title = 'stop_on_match';
        item.appendChild(stop);
      }
      item.title = [r.id, r.context ? 'context: ' + [].concat(r.context).join(', ') : 'any context',
        r.enabled ? null : 'disabled'].filter(Boolean).join(' · ');
      if (r.line) item.addEventListener('click', function () { gotoLine(r.line, 1); });
      section.appendChild(item);
    });
    root.appendChild(section);

    var refsSection = el('div', 'ed-section');
    var anyRefs = false;
    [['vars', 'Variables'], ['conditions', 'Conditions'], ['actions', 'Actions']].forEach(function (g) {
      var names = (summary.refs[g[0]] || []).filter(function (n) { return match(n.name); });
      if (!names.length) return;
      anyRefs = true;
      var det = el('details', 'ed-refs');
      if (filter) det.open = true;
      det.appendChild(el('summary', null, g[1] + ' (' + names.length + ')'));
      names.forEach(function (n) {
        var item = el('div', 'ed-item');
        item.appendChild(el('span', 'name', n.name));
        if (n.line) item.addEventListener('click', function () { gotoLine(n.line, 1); });
        det.appendChild(item);
      });
      refsSection.appendChild(det);
    });
    if (anyRefs) {
      var rh = el('h3');
      rh.appendChild(el('span', null, 'Refs'));
      refsSection.insertBefore(rh, refsSection.firstChild);
      root.appendChild(refsSection);
    }
    highlightCurrentRule();
  }

  function highlightCurrentRule() {
    if (!state.summary || state.tab !== 'rules') return;
    var row = editor.getCursorPosition().row + 1;
    var current = null;
    state.summary.rules.forEach(function (r) { if (r.line && r.line <= row) current = r.line; });
    var items = $('outline').querySelectorAll('.ed-section:first-child .ed-item');
    Array.prototype.forEach.call(items, function (item) {
      item.classList.toggle('current', current !== null && Number(item.dataset.line) === current);
    });
  }

  // ---------------------------------------------------------------------
  // Autocomplete
  // ---------------------------------------------------------------------

  var KEY_DOCS = {
    field: 'Condition: <namespace>.<name>', operator: 'Condition operator', value: 'Condition value',
    all: 'Every condition must match', any: 'At least one must match', none: 'None may match',
    type: 'Action type', params: 'Action parameters', '$ref': 'Use a block from refs'
  };

  function completion(caption, value, meta, doc, score) {
    return { caption: caption, value: value, meta: meta, docText: doc || '', score: score || 100 };
  }

  var completer = {
    identifierRegexps: [/[a-zA-Z_0-9.$\-{}]/],
    getCompletions: function (ed, session, pos, prefix, callback) {
      var cat = state.catalog;
      if (state.tab !== 'rules' || !cat) return callback(null, []);
      var line = session.getLine(pos.row).slice(0, pos.column);
      var refs = (state.summary && state.summary.refs) || { vars: [], conditions: [], actions: [] };
      var quoted = /:\s*["']/.test(line);
      var out = [];
      var m;

      if (/\$\{vars\.[\w-]*$/.test(line) || /\$\{v?a?r?s?$/.test(line)) {
        refs.vars.forEach(function (v) { out.push(completion('${vars.' + v.name + '}', '${vars.' + v.name + '}', 'var')); });
      } else if (/^\s*(?:-\s+)?field:\s*["']?[\w.]*$/.test(line)) {
        Object.keys(cat.fields).forEach(function (ns) {
          var info = cat.fields[ns];
          out.push(completion(ns + '.', ns + '.', 'namespace', info.description, 50));
          info.hints.forEach(function (h) {
            out.push(completion(ns + '.' + h, ns + '.' + h, ns, info.description));
          });
        });
      } else if (/^\s*(?:-\s+)?operator:\s*["']?[^\s"']*$/.test(line)) {
        Object.keys(cat.operators).forEach(function (op) {
          var symbolic = /^[<>=!]/.test(op);
          var value = symbolic && !quoted ? '"' + op + '"' : op;
          out.push(completion(op, value, cat.operators[op].value, cat.operators[op].description));
        });
      } else if (/^\s*(?:-\s+)?type:\s*[\w]*$/.test(line)) {
        Object.keys(cat.actions).forEach(function (t) {
          var spec = cat.actions[t];
          var params = Object.keys(spec.params);
          out.push(completion(t, t, 'action', spec.description + (params.length ? '\nparams: ' + params.join(', ') : '')));
        });
      } else if (/^\s*(?:-\s+)?\$ref:\s*["']?[\w.\-]*$/.test(line)) {
        refs.conditions.forEach(function (c) { out.push(completion('conditions.' + c.name, 'conditions.' + c.name, 'condition ref')); });
        refs.actions.forEach(function (a) { out.push(completion('actions.' + a.name, 'actions.' + a.name, 'action ref')); });
      } else if (/^\s*(?:-\s+)?context:\s*["']?[\w\-]*$/.test(line)) {
        var contexts = {};
        ((state.summary && state.summary.contexts) || []).concat(cat.schedule_contexts || []).forEach(function (c) { contexts[c] = true; });
        Object.keys(contexts).forEach(function (c) { out.push(completion(c, c, 'context')); });
      } else if ((m = /^\s*service:\s*[\w]*$/.exec(line))) {
        ['discord', 'slack', 'ntfy', 'generic'].forEach(function (s) { out.push(completion(s, s, 'notify')); });
        ['sonarr', 'radarr'].forEach(function (s) { out.push(completion(s, s, 'arr_blocklist')); });
      } else if (/^\s*priority:\s*[\w]*$/.test(line)) {
        ['skip', 'normal', 'high', 'maximum'].forEach(function (p) { out.push(completion(p, p, 'file priority')); });
      } else if (/^\s*(?:-\s+)?[\w$]*$/.test(line)) {
        var keys = {};
        Object.keys(cat.rule_keys).forEach(function (k) { keys[k] = ['rule', cat.rule_keys[k]]; });
        Object.keys(KEY_DOCS).forEach(function (k) { keys[k] = keys[k] || ['key', KEY_DOCS[k]]; });
        Object.keys(cat.actions).forEach(function (t) {
          Object.keys(cat.actions[t].params).forEach(function (p) {
            keys[p] = keys[p] || ['param', cat.actions[t].params[p].description];
          });
        });
        Object.keys(keys).forEach(function (k) { out.push(completion(k, k + ': ', keys[k][0], keys[k][1], 40)); });
      }
      callback(null, out);
    }
  };
  langTools.setCompleters([completer]);

  // ---------------------------------------------------------------------
  // Diff
  // ---------------------------------------------------------------------

  // Line diff: trim the common prefix/suffix, LCS on what's left
  function diffLines(aText, bText) {
    var a = aText.split('\n');
    var b = bText.split('\n');
    var start = 0;
    while (start < a.length && start < b.length && a[start] === b[start]) start++;
    var endA = a.length;
    var endB = b.length;
    while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB--; }

    var ops = [];
    var i;
    for (i = 0; i < start; i++) ops.push({ t: ' ', a: i + 1, b: i + 1, text: a[i] });

    var A = a.slice(start, endA);
    var B = b.slice(start, endB);
    var n = A.length;
    var m = B.length;
    if (n * m > 4000000) {
      A.forEach(function (t, k) { ops.push({ t: '-', a: start + k + 1, text: t }); });
      B.forEach(function (t, k) { ops.push({ t: '+', b: start + k + 1, text: t }); });
    } else {
      var w = m + 1;
      var L = new Uint32Array((n + 1) * w);
      for (var x = n - 1; x >= 0; x--) {
        for (var y = m - 1; y >= 0; y--) {
          L[x * w + y] = A[x] === B[y] ? L[(x + 1) * w + y + 1] + 1 : Math.max(L[(x + 1) * w + y], L[x * w + y + 1]);
        }
      }
      var p = 0;
      var q = 0;
      while (p < n || q < m) {
        if (p < n && q < m && A[p] === B[q]) {
          ops.push({ t: ' ', a: start + p + 1, b: start + q + 1, text: A[p] }); p++; q++;
        } else if (p < n && (q >= m || L[(p + 1) * w + q] >= L[p * w + q + 1])) {
          // Removals before additions within a change, as diff tools show them
          ops.push({ t: '-', a: start + p + 1, text: A[p] }); p++;
        } else {
          ops.push({ t: '+', b: start + q + 1, text: B[q] }); q++;
        }
      }
    }
    for (i = endA; i < a.length; i++) {
      ops.push({ t: ' ', a: i + 1, b: i - endA + endB + 1, text: a[i] });
    }
    return ops;
  }

  function renderDiff(container, ops, context) {
    context = context === undefined ? 3 : context;
    container.textContent = '';
    var changed = ops.map(function (o) { return o.t !== ' '; });
    if (changed.indexOf(true) === -1) {
      container.appendChild(el('div', 'diff-empty', 'No differences.'));
      return { added: 0, removed: 0 };
    }
    var show = ops.map(function (o, idx) {
      for (var k = Math.max(0, idx - context); k <= Math.min(ops.length - 1, idx + context); k++) {
        if (changed[k]) return true;
      }
      return false;
    });
    var added = 0;
    var removed = 0;
    var inHunk = false;
    ops.forEach(function (o, idx) {
      if (o.t === '+') added++;
      if (o.t === '-') removed++;
      if (!show[idx]) { inHunk = false; return; }
      if (!inHunk) {
        var first = o.a || o.b;
        container.appendChild(el('div', 'diff-hunk', '@@ line ' + first + ' @@'));
        inHunk = true;
      }
      var row = el('div', 'diff-row' + (o.t === '+' ? ' add' : o.t === '-' ? ' del' : ''));
      row.appendChild(el('span', 'ln', o.a || ''));
      row.appendChild(el('span', 'ln', o.b || ''));
      row.appendChild(el('span', 'sign', o.t));
      row.appendChild(el('span', 'txt', o.text));
      container.appendChild(row);
    });
    return { added: added, removed: removed };
  }

  function renderStats(node, stats) {
    node.textContent = '';
    node.appendChild(el('span', 'a', '+' + stats.added));
    node.appendChild(document.createTextNode(' '));
    node.appendChild(el('span', 'd', '−' + stats.removed));
    node.appendChild(document.createTextNode(' lines'));
  }

  // ---------------------------------------------------------------------
  // Modals
  // ---------------------------------------------------------------------

  function openModal(id) {
    var d = $(id);
    if (!d.open) d.showModal();
  }
  function closeModal(id) {
    var d = $(id);
    if (d.open) d.close();
  }
  Array.prototype.forEach.call(document.querySelectorAll('dialog [data-close]'), function (btn) {
    btn.addEventListener('click', function () { btn.closest('dialog').close(); });
  });

  function reviewEnabled() {
    return storage(function (s) { return s.getItem(REVIEW_KEY) !== 'off'; }, true);
  }
  $('opt-review').addEventListener('change', function (e) {
    var on = e.target.checked;
    storage(function (s) { s.setItem(REVIEW_KEY, on ? 'on' : 'off'); });
  });

  function openDiff(forSave) {
    var stats = renderDiff($('diff-view'), diffLines(state.baseline.content, rulesSession.getValue()));
    renderStats($('diff-stats'), stats);
    $('diff-title').textContent = forSave ? 'Review and save' : 'Changes';
    $('diff-sub').textContent = 'Your edits compared with rules.yml as saved ' + when(state.baseline.modified_at) + '.';
    $('opt-review').checked = reviewEnabled();
    var canSave = state.editable && isDirty() && !(state.validation && state.validation.counts.error);
    $('btn-diff-save').hidden = !canSave;
    openModal('modal-diff');
  }

  // ---------------------------------------------------------------------
  // Saving
  // ---------------------------------------------------------------------

  function requestSave() {
    if (state.tab !== 'rules' || !state.editable || state.saving) return;
    if (!isDirty()) { toast('No changes to save.'); return; }
    flushValidation().then(function () {
      if (state.validation && state.validation.counts.error) {
        expandProblems(true);
        toast('Fix ' + plural(state.validation.counts.error, 'error') + ' before saving.', 'err');
        return;
      }
      if (reviewEnabled()) openDiff(true);
      else doSave(state.baseline.etag);
    });
  }

  function doSave(etag) {
    var content = rulesSession.getValue();
    state.saving = true;
    updateState();
    return api(CFG.urls.source, {
      method: 'PUT',
      body: { content: content },
      headers: { 'If-Match': '"' + etag + '"' }
    }).then(function (r) {
      state.saving = false;
      if (r.status === 200) {
        state.baseline = { content: content, etag: r.data.etag, modified_at: r.data.modified_at };
        clearDraft();
        closeModal('modal-diff');
        closeModal('modal-conflict');
        updateStatusbar();
        toast(r.data.saved ? 'Saved — the next job uses these rules.' : 'Nothing changed on disk.', 'ok');
      } else if (r.status === 412) {
        state.conflict = r.data.current;
        closeModal('modal-diff');
        openConflict();
      } else if (r.status === 422) {
        state.validation = r.data.validation;
        renderDiagnostics();
        renderOutline();
        closeModal('modal-diff');
        expandProblems(true);
        toast(r.data.message, 'err');
      } else {
        toast(r.data.message || 'Save failed (HTTP ' + r.status + ')', 'err');
        if (r.status === 403) loadSource().then(updateState);
      }
      updateState();
    });
  }

  function openConflict() {
    var stats = renderDiff($('conflict-view'), diffLines(state.baseline.content, state.conflict.content));
    if (!stats.added && !stats.removed) {
      // Only line endings/whitespace beyond what the diff shows -- show it anyway
      $('conflict-view').textContent = '';
      $('conflict-view').appendChild(el('div', 'diff-empty', 'The file was rewritten on disk with no visible line changes.'));
    }
    openModal('modal-conflict');
  }

  $('btn-conflict-load').addEventListener('click', function () {
    var c = state.conflict;
    state.baseline = { content: c.content, etag: c.etag, modified_at: c.modified_at };
    replaceBuffer(c.content);
    clearDraft();
    closeModal('modal-conflict');
    updateStatusbar();
    toast('Loaded the version on disk. Undo (Ctrl+Z) brings your edits back.');
  });
  $('btn-conflict-overwrite').addEventListener('click', function () {
    var c = state.conflict;
    state.baseline = { content: c.content, etag: c.etag, modified_at: c.modified_at };
    doSave(c.etag);
  });

  $('btn-save').addEventListener('click', requestSave);
  $('btn-diff').addEventListener('click', function () { openDiff(false); });
  $('btn-diff-save').addEventListener('click', function () { doSave(state.baseline.etag); });

  editor.commands.addCommand({
    name: 'save',
    bindKey: { win: 'Ctrl-S', mac: 'Command-S' },
    exec: requestSave
  });
  document.addEventListener('keydown', function (e) {
    // Inside the editor, Ace's own save command already handled it
    if (e.target.closest && e.target.closest('.ace_editor')) return;
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
      e.preventDefault();
      requestSave();
    }
  });
  window.addEventListener('beforeunload', function (e) {
    if (isDirty()) { e.preventDefault(); e.returnValue = ''; }
  });

  // ---------------------------------------------------------------------
  // History
  // ---------------------------------------------------------------------

  $('btn-history').addEventListener('click', function () {
    state.historySelected = null;
    $('btn-hist-load').disabled = true;
    $('hist-stats').textContent = '';
    var view = $('hist-view');
    view.textContent = '';
    view.appendChild(el('div', 'diff-empty', 'Pick a version to compare it with the editor.'));
    var list = $('hist-list');
    list.textContent = '';
    list.appendChild(el('div', 'ed-problem-empty', 'Loading…'));
    openModal('modal-history');

    api(CFG.urls.history).then(function (r) {
      list.textContent = '';
      var versions = (r.data && r.data.versions) || [];
      if (!versions.length) {
        list.appendChild(el('div', 'ed-problem-empty', 'No earlier versions yet — one is kept each time you save here.'));
        return;
      }
      versions.forEach(function (v) {
        var item = el('div', 'hist-item');
        item.appendChild(el('span', 'when', when(v.saved_at)));
        item.appendChild(el('span', 'meta', (v.size / 1024).toFixed(1) + ' KB'));
        item.addEventListener('click', function () {
          Array.prototype.forEach.call(list.children, function (c) { c.classList.remove('selected'); });
          item.classList.add('selected');
          api(CFG.urls.history + '/' + encodeURIComponent(v.id)).then(function (vr) {
            if (!vr.ok) { toast(vr.data.message || 'Could not load that version', 'err'); return; }
            state.historySelected = vr.data;
            var stats = renderDiff(view, diffLines(rulesSession.getValue(), vr.data.content));
            renderStats($('hist-stats'), stats);
            $('btn-hist-load').disabled = state.tab !== 'rules' || !state.editable;
          });
        });
        list.appendChild(item);
      });
    });
  });

  $('btn-hist-load').addEventListener('click', function () {
    if (!state.historySelected) return;
    replaceBuffer(state.historySelected.content);
    closeModal('modal-history');
    toast('Loaded that version into the editor — save to restore it.');
  });

  // ---------------------------------------------------------------------
  // Dry run
  // ---------------------------------------------------------------------

  $('btn-dryrun').addEventListener('click', function () {
    var select = $('dry-context');
    var previous = select.value;
    select.textContent = '';
    var contexts = {};
    ((state.summary && state.summary.contexts) || []).concat((state.catalog && state.catalog.schedule_contexts) || [])
      .forEach(function (c) { contexts[c] = true; });
    var none = el('option', null, '(no context — only rules without one)');
    none.value = '';
    select.appendChild(none);
    Object.keys(contexts).sort().forEach(function (c) {
      var o = el('option', null, c);
      o.value = c;
      select.appendChild(o);
    });
    var custom = el('option', null, 'Other…');
    custom.value = '__custom__';
    select.appendChild(custom);
    if (previous) select.value = previous;
    $('dry-context-custom').hidden = select.value !== '__custom__';
    $('dry-status').textContent = isDirty() ? 'You have unsaved changes — the dry run uses the saved rules.yml.' : '';
    $('dry-result').textContent = '';
    $('btn-dry-start').disabled = false;
    openModal('modal-dryrun');
  });
  $('dry-context').addEventListener('change', function (e) {
    $('dry-context-custom').hidden = e.target.value !== '__custom__';
  });

  $('btn-dry-start').addEventListener('click', function () {
    var context = $('dry-context').value;
    if (context === '__custom__') context = $('dry-context-custom').value.trim();
    var url = CFG.urls.execute + '?dry_run=true' + (context ? '&context=' + encodeURIComponent(context) : '');
    $('btn-dry-start').disabled = true;
    $('dry-result').textContent = '';
    $('dry-status').textContent = 'Queueing…';
    api(url, { method: 'POST' }).then(function (r) {
      if (r.status !== 202) {
        $('dry-status').textContent = r.data.message || 'Could not start the dry run';
        $('btn-dry-start').disabled = false;
        return;
      }
      pollJob(r.data.job_id, 0);
    });
  });

  function pollJob(jobId, attempt) {
    api(CFG.urls.jobsApi + '/' + encodeURIComponent(jobId)).then(function (r) {
      var job = r.data;
      if (!r.ok) {
        $('dry-status').textContent = job.message || 'Lost track of the job';
        $('btn-dry-start').disabled = false;
        return;
      }
      if (job.status === 'pending' || job.status === 'processing') {
        $('dry-status').textContent = (job.status === 'pending' ? 'Waiting for the worker' : 'Running') + '…';
        if (attempt < 300) setTimeout(function () { pollJob(jobId, attempt + 1); }, attempt < 10 ? 700 : 2000);
        return;
      }
      $('btn-dry-start').disabled = false;
      var status = $('dry-status');
      status.textContent = '';
      status.appendChild(document.createTextNode('Dry run ' + job.status + ' — '));
      var link = el('a', null, 'job details');
      link.href = CFG.urls.jobsPage + '/' + encodeURIComponent(jobId);
      status.appendChild(link);
      if (job.status === 'completed' && job.result) renderRunResult(job.result);
      else if (job.error) $('dry-result').appendChild(el('pre', null, summarizeError(job.error)));
    });
  }

  // The final exception's own lines from a stored traceback -- the same
  // trimming server.py's _summarize_error() does for the job page
  function summarizeError(error) {
    var re = /^[A-Za-z_][\w.]*(?:Error|Exception|Warning)\b.*:/gm;
    var last = -1;
    var m;
    while ((m = re.exec(error)) !== null) last = m.index;
    return last === -1 ? error.trim().split('\n').pop() : error.slice(last).trim();
  }

  function renderRunResult(result) {
    state.ruleHits = result.by_rule || {};
    renderOutline();
    var box = $('dry-result');
    box.textContent = '';
    var totals = el('p', 'muted',
      plural(result.torrents_processed || 0, 'torrent') + ' checked · ' +
      (result.rules_matched || 0) + ((result.rules_matched === 1) ? ' match' : ' matches') + ' · ' +
      plural(result.actions_executed || 0, 'action') + ' would run · ' +
      (result.actions_skipped || 0) + ' skipped · ' + plural(result.errors || 0, 'error'));
    box.appendChild(totals);
    var ids = Object.keys(state.ruleHits);
    if (!ids.length) return;
    var table = el('table');
    var head = el('tr');
    ['Rule', 'Matched', 'Actions', 'Skipped', 'Errors'].forEach(function (h) { head.appendChild(el('th', null, h)); });
    table.appendChild(head);
    ids.forEach(function (id) {
      var s = state.ruleHits[id];
      var tr = el('tr');
      tr.appendChild(el('td', null, id));
      [s.matched, s.actions_executed, s.actions_skipped, s.errors].forEach(function (v) { tr.appendChild(el('td', 'num', v || 0)); });
      table.appendChild(tr);
    });
    box.appendChild(table);
  }

  // ---------------------------------------------------------------------
  // Drafts (this browser only)
  // ---------------------------------------------------------------------

  var draftTimer = null;
  function saveDraftSoon() {
    clearTimeout(draftTimer);
    draftTimer = setTimeout(function () {
      if (!isDirty()) { clearDraft(); return; }
      storage(function (s) {
        s.setItem(DRAFT_KEY, JSON.stringify({
          path: state.path, content: rulesSession.getValue(), etag: state.baseline.etag, at: new Date().toISOString()
        }));
      });
    }, 800);
  }
  function clearDraft() {
    clearTimeout(draftTimer);
    storage(function (s) { s.removeItem(DRAFT_KEY); });
    $('banner-draft').hidden = true;
  }
  function offerDraft() {
    var draft = storage(function (s) { return JSON.parse(s.getItem(DRAFT_KEY) || 'null'); }, null);
    if (!draft || draft.path !== state.path || draft.content === state.baseline.content) return;
    $('draft-text').textContent = 'You have unsaved edits from ' + when(draft.at) +
      (draft.etag !== state.baseline.etag ? ' — rules.yml has changed on disk since then.' : '.');
    $('banner-draft').hidden = false;
    $('btn-draft-restore').onclick = function () {
      replaceBuffer(draft.content);
      $('banner-draft').hidden = true;
    };
    $('btn-draft-discard').onclick = clearDraft;
  }

  // ---------------------------------------------------------------------
  // Tabs
  // ---------------------------------------------------------------------

  function selectTab(tab) {
    state.tab = tab;
    $('tab-rules').setAttribute('aria-selected', String(tab === 'rules'));
    $('tab-config').setAttribute('aria-selected', String(tab === 'config'));
    $('banner-config').hidden = tab !== 'config';
    showReadonlyBanner();
    editor.setSession(tab === 'rules' ? rulesSession : configSession);
    if (tab === 'config' && !state.config) {
      api(CFG.urls.config).then(function (r) {
        if (!r.ok) { toast(r.data.message || 'Could not load config.yml', 'err'); return; }
        state.config = r.data;
        configSession.setValue(r.data.content);
        renderDiagnostics();
        updateStatusbar();
      });
    }
    renderDiagnostics();
    renderOutline();
    updateStatusbar();
    updateState();
  }
  $('tab-rules').addEventListener('click', function () { selectTab('rules'); });
  $('tab-config').addEventListener('click', function () { selectTab('config'); });

  // ---------------------------------------------------------------------
  // Panels
  // ---------------------------------------------------------------------

  function expandProblems(open) {
    var p = $('problems');
    p.classList.toggle('collapsed', !open);
    $('problems-caret').textContent = open ? '▾' : '▸';
    $('problems-head').setAttribute('aria-expanded', String(open));
    editor.resize();
  }
  $('problems-head').addEventListener('click', function () {
    expandProblems($('problems').classList.contains('collapsed'));
  });
  $('problems-head').addEventListener('keydown', function (e) {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); expandProblems($('problems').classList.contains('collapsed')); }
  });
  ['error', 'warning', 'info'].forEach(function (sev) {
    $('count-' + sev).addEventListener('click', function () {
      expandProblems(true);
      var first = currentIssues().filter(function (i) { return i.severity === sev && i.line; })[0];
      if (first) gotoLine(first.line, first.column);
    });
  });
  $('outline-filter').addEventListener('input', renderOutline);
  $('btn-side').addEventListener('click', function () {
    $('ed-body').classList.toggle('show-side');
    editor.resize();
  });

  editor.selection.on('changeCursor', function () {
    var c = editor.getCursorPosition();
    $('sb-cursor').textContent = 'Ln ' + (c.row + 1) + ', Col ' + (c.column + 1);
    highlightCurrentRule();
  });

  rulesSession.on('change', function () {
    if (!state.baseline || state.loading) return;
    scheduleValidate();
    saveDraftSoon();
    updateState();
  });

  // Pick up changes made elsewhere when coming back to the tab
  document.addEventListener('visibilitychange', function () {
    if (document.visibilityState !== 'visible' || !state.baseline || state.saving) return;
    api(CFG.urls.source).then(function (r) {
      if (!r.ok || r.data.etag === state.baseline.etag) return;
      if (!isDirty()) {
        state.baseline = { content: r.data.content, etag: r.data.etag, modified_at: r.data.modified_at };
        replaceBuffer(r.data.content);
        updateStatusbar();
        toast('rules.yml changed on disk — reloaded.');
      } else {
        toast('rules.yml changed on disk since you opened it. Saving will ask what to do.', 'err');
      }
    });
  });

  // ---------------------------------------------------------------------
  // Start
  // ---------------------------------------------------------------------

  api(CFG.urls.catalog).then(function (r) { if (r.ok) state.catalog = r.data; });

  loadSource().then(function (data) {
    if (!data) return;
    rulesSession.setValue(data.content);
    rulesSession.getUndoManager().reset();
    state.loading = false;
    offerDraft();
    updateState();
    runValidate().then(function () {
      var m = /rule=([^&]+)/.exec(location.hash);
      if (m && state.summary) {
        var id = decodeURIComponent(m[1]);
        var rule = state.summary.rules.filter(function (r) { return r.id === id; })[0];
        if (rule && rule.line) gotoLine(rule.line, 1);
      }
    });
    editor.focus();
  });
})();
