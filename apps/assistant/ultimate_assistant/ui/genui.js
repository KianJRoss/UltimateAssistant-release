/* Canvas UI: renders the interactive "ui" canvas elements.
 *
 * The assistant writes a card as text: Markdown, plus Svelte-style blocks
 * ({#if}, {:else if}, {:else}, {/if}, {#each list as item, i}, {/each},
 * {@body statements}, {expression}) and JSX-style components (<box>, <row>,
 * <slider value={x} onChange={setX}/>, <Chart>, <svg> ...). It is the same
 * language ChatGPT writes in its replies (docs/research/chatgpt-genui), so
 * models already know it; canvas_guide.md is the reference agents read.
 *
 * State lives in the card (DIL.useState); clicks and typing run here without
 * a model round trip. UA.* sends things back to the assistant (canvas.py
 * events), GenUI.* is kept for compatibility.
 *
 * Usage: GenUICard.mount(rootElement, element, {base, token}) and later
 * card.update(element) when the assistant changes it. A host app without
 * HTTP access (the phone's WebView) passes {onEvent, onState} instead.
 */
(function (global) {
  'use strict';

  // ---------------------------------------------------------------- parsing

  const RAW_TAGS = new Set(['CodeBlock', 'pre', 'code', 'style', 'script']);
  const VOID_TAGS = new Set(['br', 'hr', 'img', 'input', 'meta', 'link']);

  function skipString(s, i) {
    const quote = s[i++];
    while (i < s.length && s[i] !== quote) i += s[i] === '\\' ? 2 : 1;
    return i + 1;
  }

  function skipTemplate(s, i) {
    i++;
    while (i < s.length) {
      if (s[i] === '\\') { i += 2; continue; }
      if (s[i] === '`') return i + 1;
      if (s[i] === '$' && s[i + 1] === '{') { i = matchBrace(s, i + 1) + 1; continue; }
      i++;
    }
    return i;
  }

  /** Index of the bracket closing the one at s[open], skipping JS strings and comments. */
  function matchBrace(s, open) {
    const closers = { '(': ')', '[': ']', '{': '}' };
    const stack = [closers[s[open]]];
    let i = open + 1;
    while (i < s.length) {
      const c = s[i];
      if (c === '"' || c === "'") { i = skipString(s, i); continue; }
      if (c === '`') { i = skipTemplate(s, i); continue; }
      if (c === '/' && s[i + 1] === '*') { const end = s.indexOf('*/', i + 2); i = end < 0 ? s.length : end + 2; continue; }
      if (closers[c]) { stack.push(closers[c]); i++; continue; }
      if (c === ')' || c === ']' || c === '}') {
        stack.pop();
        if (!stack.length) return i;
      }
      i++;
    }
    throw new Error('Unclosed ' + s[open] + ' at ' + open);
  }

  class Parser {
    constructor(src) { this.s = src; this.i = 0; }

    /** Children until a closing tag or a block keyword; returns {nodes, stop}. */
    children(closeTag) {
      const s = this.s, nodes = [];
      let text = '';
      const flush = () => { if (text) { nodes.push({ t: 'text', v: text }); text = ''; } };
      while (this.i < s.length) {
        const i = this.i, c = s[i];
        if (s.startsWith('```', i)) {
          const end = s.indexOf('\n```', i + 3);
          const stop = end < 0 ? s.length : end + 4;
          text += s.slice(i, stop); this.i = stop; continue;
        }
        if (c === '`') {
          const end = s.indexOf('`', i + 1);
          const line = s.indexOf('\n', i + 1);
          if (end > 0 && (line < 0 || end < line)) { text += s.slice(i, end + 1); this.i = end + 1; continue; }
        }
        if (c === '\\' && (s[i + 1] === '[' || s[i + 1] === '(')) {
          const close = s[i + 1] === '[' ? '\\]' : '\\)';
          const end = s.indexOf(close, i + 2);
          if (end > 0) {
            flush();
            nodes.push({ t: 'math', v: s.slice(i + 2, end).trim(), display: s[i + 1] === '[' });
            this.i = end + 2; continue;
          }
        }
        if (c === '$' && s[i + 1] === '$') {
          const end = s.indexOf('$$', i + 2);
          if (end > 0) { flush(); nodes.push({ t: 'math', v: s.slice(i + 2, end).trim(), display: true }); this.i = end + 2; continue; }
        }
        if (c === '\\' && (s[i + 1] === '{' || s[i + 1] === '<')) { text += s[i + 1]; this.i = i + 2; continue; }
        if (c === '{') {
          const next = s[i + 1];
          if (next === '/' || next === ':') {
            flush();
            const end = matchBrace(s, i);
            const word = s.slice(i + 1, end).trim();
            this.i = end + 1;
            if (word.startsWith(':else if ')) return { nodes, stop: { kind: 'elseif', cond: word.slice(9) } };
            if (word === ':else') return { nodes, stop: { kind: 'else' } };
            return { nodes, stop: { kind: 'end', word: word.slice(1) } };
          }
          if (next === '#') { flush(); nodes.push(this.block()); continue; }
          if (next === '@') {
            flush();
            const end = matchBrace(s, i);
            const inner = s.slice(i + 2, end);
            const space = inner.search(/\s/);
            const kind = space < 0 ? inner : inner.slice(0, space);
            const code = space < 0 ? '' : inner.slice(space + 1);
            if (kind === 'body' || kind === 'const') nodes.push({ t: 'body', code: kind === 'const' ? 'const ' + code : code });
            else if (kind === 'html') nodes.push({ t: 'html', code });
            this.i = end + 1; continue;
          }
          if (/\s/.test(next || ' ')) { text += c; this.i++; continue; }
          flush();
          const end = matchBrace(s, i);
          nodes.push({ t: 'expr', code: s.slice(i + 1, end) });
          this.i = end + 1; continue;
        }
        if (c === '<') {
          if (s.startsWith('<!--', i)) { const end = s.indexOf('-->', i); this.i = end < 0 ? s.length : end + 3; continue; }
          if (s[i + 1] === '/' && /[A-Za-z]/.test(s[i + 2] || '')) {
            flush();
            const end = s.indexOf('>', i);
            this.i = end + 1;
            return { nodes, stop: { kind: 'close', tag: s.slice(i + 2, end).trim() } };
          }
          if (/[A-Za-z]/.test(s[i + 1] || '')) { flush(); nodes.push(this.element()); continue; }
        }
        text += c; this.i++;
      }
      flush();
      if (closeTag) return { nodes, stop: { kind: 'eof' } };
      return { nodes, stop: null };
    }

    block() {
      const s = this.s, start = this.i, end = matchBrace(s, start);
      const head = s.slice(start + 2, end).trim();
      this.i = end + 1;
      if (head.startsWith('if ')) {
        const branches = [];
        let cond = head.slice(3), result;
        for (;;) {
          result = this.children('if');
          branches.push({ cond, nodes: result.nodes });
          if (!result.stop || result.stop.kind === 'end' || result.stop.kind === 'eof') break;
          if (result.stop.kind === 'elseif') { cond = result.stop.cond; continue; }
          if (result.stop.kind === 'else') { cond = null; continue; }
          break;
        }
        return { t: 'if', branches };
      }
      if (head.startsWith('each ')) {
        const body = head.slice(5);
        const at = body.lastIndexOf(' as ');
        const list = body.slice(0, at);
        let pattern = body.slice(at + 4).trim(), key = null, index = null;
        const keyMatch = pattern.match(/\(([^()]*)\)\s*$/);
        if (keyMatch) { key = keyMatch[1]; pattern = pattern.slice(0, keyMatch.index).trim(); }
        let depth = 0, split = -1;
        for (let k = 0; k < pattern.length; k++) {
          const ch = pattern[k];
          if (ch === '{' || ch === '[') depth++;
          else if (ch === '}' || ch === ']') depth--;
          else if (ch === ',' && depth === 0) split = k;
        }
        if (split >= 0) { index = pattern.slice(split + 1).trim(); pattern = pattern.slice(0, split).trim(); }
        const result = this.children('each');
        let empty = null;
        if (result.stop && result.stop.kind === 'else') empty = this.children('each').nodes;
        return { t: 'each', list, pattern, index, key, nodes: result.nodes, empty };
      }
      if (head.startsWith('key ')) {
        return { t: 'fragment', nodes: this.children('key').nodes };
      }
      return { t: 'text', v: '' };
    }

    element() {
      const s = this.s;
      let i = this.i + 1;
      const nameMatch = /^[A-Za-z][\w.:-]*/.exec(s.slice(i));
      const tag = nameMatch[0];
      i += tag.length;
      const attrs = [];
      let selfClosing = false;
      while (i < s.length) {
        while (/\s/.test(s[i])) i++;
        if (s[i] === '/' && s[i + 1] === '>') { selfClosing = true; i += 2; break; }
        if (s[i] === '>') { i++; break; }
        if (s[i] === '{') {  // {...spread}
          const end = matchBrace(s, i);
          const code = s.slice(i + 1, end).trim();
          if (code.startsWith('...')) attrs.push({ name: '...', kind: 'expr', v: code.slice(3) });
          i = end + 1; continue;
        }
        const attr = /^[A-Za-z_:@][\w:.-]*/.exec(s.slice(i));
        if (!attr) { i++; continue; }
        const name = attr[0];
        i += name.length;
        while (/\s/.test(s[i])) i++;
        if (s[i] !== '=') { attrs.push({ name, kind: 'bool' }); continue; }
        i++;
        while (/\s/.test(s[i])) i++;
        if (s[i] === '"' || s[i] === "'") {
          const end = skipString(s, i);
          attrs.push({ name, kind: 'str', v: s.slice(i + 1, end - 1) });
          i = end;
        } else if (s[i] === '{') {
          const end = matchBrace(s, i);
          attrs.push({ name, kind: 'expr', v: s.slice(i + 1, end) });
          i = end + 1;
        } else {
          const bare = /^[^\s/>]+/.exec(s.slice(i))[0];
          attrs.push({ name, kind: 'str', v: bare });
          i += bare.length;
        }
      }
      this.i = i;
      const node = { t: 'el', tag, attrs, nodes: [] };
      if (selfClosing || VOID_TAGS.has(tag)) return node;
      if (RAW_TAGS.has(tag)) {
        const close = s.indexOf('</' + tag + '>', i);
        const end = close < 0 ? s.length : close;
        node.raw = s.slice(i, end);
        this.i = close < 0 ? s.length : close + tag.length + 3;
        return node;
      }
      node.nodes = this.children(tag).nodes;
      return node;
    }
  }

  const parseCache = new Map();
  function parse(src) {
    if (parseCache.has(src)) return parseCache.get(src);
    const tree = new Parser(src).children(null).nodes;
    if (parseCache.size > 50) parseCache.clear();
    parseCache.set(src, tree);
    return tree;
  }

  // ------------------------------------------------------------- evaluation

  const fnCache = new Map();
  function compile(key, make) {
    let fn = fnCache.get(key);
    if (!fn) { fn = make(); fnCache.set(key, fn); }
    return fn;
  }

  function evaluate(code, scope) {
    const fn = compile('e:' + code, () => new Function('__scope', 'with (__scope) { return (' + code + '\n); }'));
    return fn(scope);
  }

  /** Names declared at the top level of a statement block (const/let/var/function). */
  function declaredNames(code) {
    const names = [];
    let depth = 0;
    for (let i = 0; i < code.length; i++) {
      const c = code[i];
      if (c === '"' || c === "'") { i = skipString(code, i) - 1; continue; }
      if (c === '`') { i = skipTemplate(code, i) - 1; continue; }
      if (c === '{' || c === '(' || c === '[') {
        if (depth === 0 && c !== '(' && /(?:const|let|var)\s*$/.test(code.slice(0, i))) {
          const end = matchBrace(code, i);
          code.slice(i + 1, end).split(',').forEach((part) => {
            const name = part.split('=')[0].split(':').pop().replace('...', '').trim();
            if (/^[A-Za-z_$][\w$]*$/.test(name)) names.push(name);
          });
          i = end; continue;
        }
        depth++; continue;
      }
      if (c === '}' || c === ')' || c === ']') { depth--; continue; }
      if (depth === 0 && /[A-Za-z_$]/.test(c) && !/[\w$.]/.test(code[i - 1] || ' ')) {
        const word = /^[A-Za-z_$][\w$]*/.exec(code.slice(i))[0];
        if (word === 'const' || word === 'let' || word === 'var' || word === 'function') {
          const rest = code.slice(i + word.length);
          const name = /^\s*\*?\s*([A-Za-z_$][\w$]*)/.exec(rest);
          if (name) names.push(name[1]);
        }
        i += word.length - 1;
      }
    }
    return names;
  }

  function runBody(code, scope) {
    const fn = compile('b:' + code, () => {
      const names = declaredNames(code);
      return new Function('__scope', 'with (__scope) {\n' + code + '\n;return {' + names.join(',') + '};\n}');
    });
    Object.assign(scope, fn(scope));
  }

  function bindPattern(pattern, value, scope) {
    if (/^[A-Za-z_$][\w$]*$/.test(pattern)) { scope[pattern] = value; return; }
    const fn = compile('p:' + pattern, () => new Function('__v', 'const ' + pattern + ' = __v; return {' +
      declaredNames('const ' + pattern + ' = 0').join(',') + '};'));
    Object.assign(scope, fn(value));
  }

  // --------------------------------------------------------------- markdown

  const PH_OPEN = '', PH_CLOSE = '';
  const PH = new RegExp(PH_OPEN + '(\\d+)' + PH_CLOSE, 'g');

  function escapeHtml(text) {
    return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function inlineMarkdown(text) {
    const codes = [];
    let out = escapeHtml(text).replace(/`([^`]+)`/g, (_, code) => { codes.push(code); return '' + (codes.length - 1) + ''; });
    out = out
      .replace(/\*\*([^*]+?)\*\*/g, '<strong>$1</strong>')
      .replace(/__([^_]+?)__/g, '<strong>$1</strong>')
      .replace(/(^|[^*\w])\*([^*\s][^*]*?)\*(?!\w)/g, '$1<em>$2</em>')
      .replace(/(^|[^\w])_([^_\s][^_]*?)_(?!\w)/g, '$1<em>$2</em>')
      .replace(/~~([^~]+?)~~/g, '<del>$1</del>')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, label, url) =>
        /^(https?:|mailto:)/.test(url) ? '<a href="' + url + '" data-url="' + url + '">' + label + '</a>' : label);
    return out.replace(/(\d+)/g, (_, n) => '<code>' + codes[Number(n)] + '</code>');
  }

  function tableRow(line) {
    return line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map((cell) => cell.trim());
  }

  function blockMarkdown(text) {
    const lines = text.split('\n');
    const html = [];
    let paragraph = [];
    const endParagraph = () => {
      if (paragraph.length) html.push('<p>' + inlineMarkdown(paragraph.join(' ')) + '</p>');
      paragraph = [];
    };
    for (let i = 0; i < lines.length; i++) {
      const raw = lines[i], line = raw.trim();
      if (!line) { endParagraph(); continue; }
      if (line.startsWith('```')) {
        endParagraph();
        const lang = line.slice(3).trim();
        const body = [];
        while (++i < lines.length && !lines[i].trim().startsWith('```')) body.push(lines[i]);
        html.push('<pre class="ua-code"><code data-lang="' + escapeHtml(lang) + '">' + escapeHtml(dedent(body.join('\n'))) + '</code></pre>');
        continue;
      }
      let match;
      if ((match = /^(#{1,6})\s+(.*)$/.exec(line))) {
        endParagraph();
        html.push('<h' + match[1].length + '>' + inlineMarkdown(match[2]) + '</h' + match[1].length + '>');
        continue;
      }
      if (/^(-{3,}|\*{3,}|_{3,})$/.test(line)) { endParagraph(); html.push('<hr>'); continue; }
      if (line.startsWith('>')) {
        endParagraph();
        const quote = [];
        while (i < lines.length && lines[i].trim().startsWith('>')) quote.push(lines[i++].trim().replace(/^>\s?/, ''));
        i--;
        html.push('<blockquote>' + blockMarkdown(quote.join('\n')) + '</blockquote>');
        continue;
      }
      if (line.startsWith('|') && i + 1 < lines.length && /^\|?\s*:?-{2,}/.test(lines[i + 1].trim())) {
        endParagraph();
        const head = tableRow(line);
        const align = tableRow(lines[i + 1]).map((c) => (c.endsWith(':') ? (c.startsWith(':') ? 'center' : 'right') : 'left'));
        i += 2;
        const rows = [];
        while (i < lines.length && lines[i].trim().startsWith('|')) rows.push(tableRow(lines[i++]));
        i--;
        const cell = (tag, text, k) => '<' + tag + ' style="text-align:' + (align[k] || 'left') + '">' + inlineMarkdown(text) + '</' + tag + '>';
        html.push('<table class="ua-md-table"><thead><tr>' + head.map((t, k) => cell('th', t, k)).join('') + '</tr></thead><tbody>' +
          rows.map((r) => '<tr>' + r.map((t, k) => cell('td', t, k)).join('') + '</tr>').join('') + '</tbody></table>');
        continue;
      }
      if ((match = /^([-*+]|\d+[.)])\s+(.*)$/.exec(line))) {
        endParagraph();
        const ordered = /\d/.test(match[1]);
        const items = [];
        while (i < lines.length) {
          const item = /^([-*+]|\d+[.)])\s+(.*)$/.exec(lines[i].trim());
          if (item && /\d/.test(item[1]) === ordered) { items.push(item[2]); i++; continue; }
          if (lines[i].trim() && /^\s{2,}/.test(lines[i]) && items.length) { items[items.length - 1] += ' ' + lines[i].trim(); i++; continue; }
          break;
        }
        i--;
        const tag = ordered ? 'ol' : 'ul';
        html.push('<' + tag + '>' + items.map((t) => '<li>' + inlineMarkdown(t) + '</li>').join('') + '</' + tag + '>');
        continue;
      }
      paragraph.push(line);
    }
    endParagraph();
    return html.join('');
  }

  function dedent(text) {
    const lines = text.replace(/^\n+/, '').replace(/\s+$/, '').split('\n');
    const indent = Math.min(...lines.filter((l) => l.trim()).map((l) => /^\s*/.exec(l)[0].length));
    return lines.map((l) => l.slice(Number.isFinite(indent) ? indent : 0)).join('\n');
  }

  // ------------------------------------------------------------ the toolkit

  const SVG_NS = 'http://www.w3.org/2000/svg';
  const SVG_TAGS = new Set(['svg', 'g', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text',
    'tspan', 'defs', 'linearGradient', 'radialGradient', 'stop', 'clipPath', 'mask', 'pattern', 'use', 'title',
    'marker', 'filter', 'feGaussianBlur', 'feOffset', 'feBlend', 'feDropShadow', 'image', 'foreignObject', 'symbol']);
  const SVG_KEEP_CASE = new Set(['viewBox', 'preserveAspectRatio', 'gradientUnits', 'gradientTransform', 'patternUnits',
    'markerWidth', 'markerHeight', 'refX', 'refY', 'stdDeviation', 'textLength', 'pathLength', 'clipPathUnits']);

  const COLORS = {
    default: 'var(--ua-ink)', primary: 'var(--ua-accent)', secondary: 'var(--ua-ink-2)', tertiary: 'var(--ua-ink-3)',
    success: 'var(--ua-success)', danger: 'var(--ua-danger)', error: 'var(--ua-danger)', warning: 'var(--ua-warning)',
    discovery: 'var(--ua-discovery)', info: 'var(--ua-accent)', subtle: 'var(--ua-line)', inverse: 'var(--ua-bg)',
  };
  const SURFACES = {
    surface: 'var(--ua-surface)', 'surface-secondary': 'var(--ua-surface-2)', 'surface-tertiary': 'var(--ua-surface-3)',
    'surface-elevated': 'var(--ua-surface-2)', transparent: 'transparent',
  };
  const RADII = { none: '0', xs: '4px', sm: '6px', md: '8px', lg: '12px', xl: '16px', '2xl': '20px', '3xl': '28px', full: '9999px' };
  const TEXT_SIZES = { '2xs': '10px', xs: '12px', sm: '13px', md: '15px', base: '15px', lg: '17px', xl: '20px', '2xl': '24px', '3xl': '30px', '4xl': '36px' };
  const ICON_SIZES = { xs: 12, sm: 14, md: 16, lg: 20, xl: 24, '2xl': 32, '3xl': 40 };
  const WEIGHTS = { regular: 400, normal: 400, medium: 500, semibold: 600, bold: 700 };
  const ALIGN = { start: 'flex-start', end: 'flex-end', center: 'center', stretch: 'stretch', baseline: 'baseline' };
  const JUSTIFY = { start: 'flex-start', end: 'flex-end', center: 'center', between: 'space-between', around: 'space-around', evenly: 'space-evenly' };
  const PALETTE = ['#7aa2f7', '#41b88f', '#e0af68', '#f7768e', '#bb9af7', '#2ac3de', '#ff9e64', '#9ece6a'];

  const space = (v) => (v === undefined || v === null || v === '' ? '' : typeof v === 'number' ? v * 4 + 'px' : /^\d+$/.test(v) ? Number(v) * 4 + 'px' : v);
  const size = (v) => (v === undefined || v === null ? '' : typeof v === 'number' ? v + 'px' : v);
  const color = (v) => (v in COLORS ? COLORS[v] : v);
  const surface = (v) => (v in SURFACES ? SURFACES[v] : v in COLORS ? COLORS[v] : v);

  function h(tag, className, style) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (style) Object.assign(el.style, style);
    return el;
  }

  /** Handlers get (event, liveNode): after a re-render the live node is not the one built here. */
  function on(el, type, handler) {
    if (!handler) return el;
    (el.__ua || (el.__ua = {}))[type] = handler;
    return el;
  }

  /** Shared box styling: layout, spacing, surface, border, radius, size. */
  function boxStyle(el, p, direction) {
    const st = el.style;
    if (direction) { st.display = 'flex'; st.flexDirection = direction; }
    if (p.gap !== undefined) st.gap = space(p.gap);
    if (p.padding !== undefined) st.padding = space(p.padding);
    for (const side of ['paddingTop', 'paddingBottom', 'paddingLeft', 'paddingRight', 'margin', 'marginTop', 'marginBottom']) {
      if (p[side] !== undefined) st[side] = space(p[side]);
    }
    if (p.paddingX !== undefined) st.paddingLeft = st.paddingRight = space(p.paddingX);
    if (p.paddingY !== undefined) st.paddingTop = st.paddingBottom = space(p.paddingY);
    if (p.align) st.alignItems = ALIGN[p.align] || p.align;
    if (p.justify) st.justifyContent = JUSTIFY[p.justify] || p.justify;
    if (p.wrap) st.flexWrap = p.wrap === true ? 'wrap' : p.wrap;
    if (p.flex !== undefined) st.flex = String(p.flex) === '1' ? '1 1 0' : String(p.flex);
    if (p.flex !== undefined) st.minWidth = '0';
    if (p.background) st.background = surface(p.background);
    if (p.border) {
      if (p.border === true || p.border === 'true' || p.border === 'default') st.border = '1px solid var(--ua-line)';
      else if (typeof p.border === 'object') st.border = (p.border.size || 1) + 'px ' + (p.border.style || 'solid') + ' ' + color(p.border.color || 'subtle');
      else st.border = '1px solid ' + color(p.border);
    }
    if (p.radius) st.borderRadius = RADII[p.radius] || size(p.radius);
    if (p.clip) st.overflow = 'hidden';
    if (p.scroll) st.overflow = 'auto';
    for (const key of ['width', 'height', 'minWidth', 'maxWidth', 'minHeight', 'maxHeight']) if (p[key] !== undefined) st[key] = size(p[key]);
    if (p.aspectRatio) st.aspectRatio = String(p.aspectRatio).replace(':', '/');
    if (p.textAlign) st.textAlign = p.textAlign;
    if (p.opacity !== undefined) st.opacity = p.opacity;
    if (p.shadow) st.boxShadow = '0 6px 24px rgba(0,0,0,.35)';
    if (p.tooltip) el.title = p.tooltip;
    if (p.style && typeof p.style === 'object') Object.assign(st, p.style);
    if (p.onClick) { on(el, 'click', p.onClick); el.classList.add('ua-clickable'); }
    return el;
  }

  function textStyle(el, p) {
    if (p.size) el.style.fontSize = TEXT_SIZES[p.size] || size(p.size);
    if (p.color) el.style.color = color(p.color);
    if (p.weight) el.style.fontWeight = WEIGHTS[p.weight] || p.weight;
    if (p.tabularNums) el.style.fontVariantNumeric = 'tabular-nums';
    if (p.textAlign) el.style.textAlign = p.textAlign;
    if (p.italic) el.style.fontStyle = 'italic';
    if (p.truncate) el.classList.add('ua-truncate');
    if (p.maxLines) { el.classList.add('ua-clamp'); el.style.webkitLineClamp = p.maxLines; }
    return el;
  }

  function options(list) {
    return (list || []).map((o) => (typeof o === 'object' ? o : { label: String(o), value: o }));
  }

  // Icons come from Lucide (loaded once); until it arrives an icon is an empty square.
  const ICON_ALIASES = {
    home: 'house', 'alert-triangle': 'triangle-alert', 'alert-circle': 'circle-alert', 'check-circle': 'circle-check',
    'check-circle-2': 'circle-check-big', 'x-circle': 'circle-x', 'help-circle': 'circle-help', 'bar-chart': 'chart-bar',
    'bar-chart-2': 'chart-column', 'pie-chart': 'chart-pie', 'line-chart': 'chart-line', 'more-horizontal': 'ellipsis',
    'more-vertical': 'ellipsis-vertical', 'edit': 'pencil', 'trash': 'trash-2', 'settings-2': 'settings-2',
  };
  function iconNode(name) {
    const lib = global.lucide && global.lucide.icons;
    if (!lib) return null;
    const pascal = (n) => n.split('-').map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join('');
    return lib[pascal(name)] || lib[pascal(ICON_ALIASES[name] || '')] || null;
  }

  function icon(p) {
    const px = typeof p.size === 'number' ? p.size : ICON_SIZES[p.size || 'md'] || 16;
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', px);
    svg.setAttribute('height', px);
    svg.setAttribute('fill', p.fill && p.fill !== 'none' ? color(p.fill) : 'none');
    svg.setAttribute('stroke', p.color ? color(p.color) : 'currentColor');
    svg.setAttribute('stroke-width', p.strokeWidth || 2);
    svg.setAttribute('stroke-linecap', 'round');
    svg.setAttribute('stroke-linejoin', 'round');
    svg.setAttribute('class', 'ua-icon' + (p.inline ? ' ua-icon-inline' : ''));
    svg.dataset.icon = p.name || '';
    let node = iconNode(p.name || '');
    if (node) {
      if (node[0] === 'svg') node = node[2];
      for (const [tag, attrs] of node) {
        const child = document.createElementNS(SVG_NS, tag);
        for (const key in attrs) if (key !== 'key') child.setAttribute(key, attrs[key]);
        svg.appendChild(child);
      }
    } else {
      const rect = document.createElementNS(SVG_NS, 'rect');
      rect.setAttribute('x', 5); rect.setAttribute('y', 5); rect.setAttribute('width', 14); rect.setAttribute('height', 14);
      rect.setAttribute('rx', 4); rect.setAttribute('opacity', '.35');
      svg.appendChild(rect);
    }
    return svg;
  }

  // ------------------------------------------------------------------ charts

  function chart(content) {
    const c = content || {};
    const data = c.data || [];
    const series = (c.series && c.series.length ? c.series : [{ dataKey: c.valueKey || 'value' }]);
    const W = 360, H = 200, L = 34, R = 10, T = 12, B = 28;
    const wrap = h('div', 'ua-chart');
    const svg = document.createElementNS(SVG_NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('width', '100%');
    const add = (tag, attrs, text) => {
      const el = document.createElementNS(SVG_NS, tag);
      for (const k in attrs) el.setAttribute(k, attrs[k]);
      if (text !== undefined) el.textContent = text;
      svg.appendChild(el);
      return el;
    };
    const tip = (el, text) => { const t = document.createElementNS(SVG_NS, 'title'); t.textContent = text; el.appendChild(t); };
    const type = c.chartType || 'bar';
    if (type === 'pie' || type === 'donut') {
      const key = c.valueKey || series[0].dataKey, nameKey = c.nameKey || c.xKey || 'name';
      const total = data.reduce((s, d) => s + (Number(d[key]) || 0), 0) || 1;
      let angle = -Math.PI / 2;
      const cx = 100, cy = H / 2, r = 80;
      data.forEach((d, k) => {
        const slice = (Number(d[key]) || 0) / total * Math.PI * 2;
        const x1 = cx + r * Math.cos(angle), y1 = cy + r * Math.sin(angle);
        angle += slice;
        const x2 = cx + r * Math.cos(angle), y2 = cy + r * Math.sin(angle);
        const path = add('path', { d: `M${cx},${cy} L${x1},${y1} A${r},${r} 0 ${slice > Math.PI ? 1 : 0} 1 ${x2},${y2} Z`, fill: PALETTE[k % PALETTE.length], stroke: 'var(--ua-surface)', 'stroke-width': 1.5 });
        tip(path, `${d[nameKey]}: ${d[key]}`);
        add('rect', { x: 210, y: 20 + k * 20, width: 10, height: 10, rx: 2, fill: PALETTE[k % PALETTE.length] });
        add('text', { x: 226, y: 29 + k * 20, 'font-size': 11, fill: 'var(--ua-ink-2)' }, `${d[nameKey]} (${Math.round((Number(d[key]) || 0) / total * 100)}%)`);
      });
      if (type === 'donut') add('circle', { cx, cy, r: 44, fill: 'var(--ua-surface)' });
      wrap.appendChild(svg);
      return wrap;
    }
    const xKey = c.xKey || 'x';
    const values = data.flatMap((d) => series.map((s) => Number(d[s.dataKey]) || 0));
    const max = Math.max(1, ...values) * 1.08, min = Math.min(0, ...values);
    const y = (v) => T + (H - T - B) * (1 - (v - min) / (max - min));
    for (let k = 0; k <= 4; k++) {
      const v = min + (max - min) * k / 4;
      add('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: 'var(--ua-line)', 'stroke-width': 0.6 });
      add('text', { x: L - 4, y: y(v) + 3, 'font-size': 9, 'text-anchor': 'end', fill: 'var(--ua-ink-3)' }, +v.toFixed(v < 10 ? 1 : 0));
    }
    const numericX = type === 'scatter';
    const xs = data.map((d) => Number(d[xKey]) || 0);
    const xMin = Math.min(...xs), xMax = Math.max(...xs);
    const band = (W - L - R) / Math.max(data.length, 1);
    const x = (d, k) => numericX ? L + (W - L - R) * ((Number(d[xKey]) - xMin) / ((xMax - xMin) || 1)) : L + band * (k + 0.5);
    data.forEach((d, k) => {
      if (!numericX || k % Math.ceil(data.length / 8) === 0) {
        add('text', { x: x(d, k), y: H - 10, 'font-size': 9, 'text-anchor': 'middle', fill: 'var(--ua-ink-3)' }, d[xKey]);
      }
    });
    series.forEach((s, n) => {
      const col = s.color || PALETTE[n % PALETTE.length];
      if (type === 'bar') {
        const bw = band * 0.7 / series.length;
        data.forEach((d, k) => {
          const v = Number(d[s.dataKey]) || 0;
          const bar = add('rect', { x: L + band * k + band * 0.15 + bw * n, y: Math.min(y(v), y(0)), width: bw, height: Math.abs(y(0) - y(v)), rx: 2, fill: col });
          tip(bar, `${d[xKey]} · ${s.label || s.dataKey}: ${v}`);
        });
      } else {
        if (type !== 'scatter') {
          const points = data.map((d, k) => `${k ? 'L' : 'M'}${x(d, k)},${y(Number(d[s.dataKey]) || 0)}`).join(' ');
          if (type === 'area') add('path', { d: points + ` L${x(data[data.length - 1], data.length - 1)},${y(0)} L${x(data[0], 0)},${y(0)} Z`, fill: col, opacity: 0.18 });
          add('path', { d: points, fill: 'none', stroke: col, 'stroke-width': 2, 'stroke-linejoin': 'round' });
        }
        data.forEach((d, k) => {
          const dot = add('circle', { cx: x(d, k), cy: y(Number(d[s.dataKey]) || 0), r: type === 'scatter' ? 4 : 2.5, fill: col });
          tip(dot, `${d[xKey]} · ${s.label || s.dataKey}: ${d[s.dataKey]}`);
        });
      }
    });
    wrap.appendChild(svg);
    if (series.length > 1 || series[0].label) {
      const legend = h('div', 'ua-legend');
      series.forEach((s, n) => {
        const item = h('span');
        const swatch = h('i', '', { background: s.color || PALETTE[n % PALETTE.length] });
        item.append(swatch, document.createTextNode(s.label || s.dataKey));
        legend.appendChild(item);
      });
      wrap.appendChild(legend);
    }
    if (c.xAxisLabel) wrap.appendChild(h('div', 'ua-caption')).textContent = c.xAxisLabel;
    return wrap;
  }

  // ------------------------------------------------------------- components

  /** Each component: (props, kids, ctx, node) -> DOM. kids(inline?) renders the children. */
  const COMPONENTS = {
    box: (p, kids) => { const el = boxStyle(h('div', 'ua-box'), p, p.direction === 'row' ? 'row' : 'column'); el.append(...kids()); return el; },
    row: (p, kids) => { const el = boxStyle(h('div', 'ua-row'), p, 'row'); if (!p.align) el.style.alignItems = 'center'; el.append(...kids()); return el; },
    col: (p, kids, ctx) => COMPONENTS.box(p, kids, ctx),
    stack: (p, kids, ctx) => COMPONENTS.box(p, kids, ctx),
    card: (p, kids) => {
      const el = boxStyle(h('div', 'ua-card'), Object.assign({ padding: 3, radius: 'xl', gap: 2 }, p), 'column');
      el.append(...kids()); return el;
    },
    spacer: (p) => h('div', '', { flex: '1 1 auto', minWidth: size(p.size) || '0', minHeight: size(p.size) || '0' }),
    divider: (p) => { const el = h('hr', 'ua-divider'); if (p.color) el.style.borderColor = color(p.color); return el; },
    grid: (p, kids) => {
      const el = boxStyle(h('div', 'ua-grid'), p);
      el.style.display = 'grid';
      el.style.gridTemplateColumns = typeof p.columns === 'string' && !/^\d+$/.test(p.columns) ? p.columns : `repeat(${p.columns || 2}, minmax(0, 1fr))`;
      el.append(...kids()); return el;
    },
    'grid-item': (p, kids) => {
      const el = boxStyle(h('div', 'ua-grid-item'), p, 'column');
      if (p.span) el.style.gridColumn = `span ${p.span}`;
      if (p.rowSpan) el.style.gridRow = `span ${p.rowSpan}`;
      el.append(...kids()); return el;
    },
    flow: (p, kids, ctx) => COMPONENTS.grid(p, kids, ctx),
    'flow-item': (p, kids, ctx) => COMPONENTS['grid-item'](p, kids, ctx),
    pressable: (p, kids) => {
      const el = boxStyle(h('div', 'ua-pressable'), p, 'column');
      el.tabIndex = 0;
      el.append(...kids()); return el;
    },
    title: (p, kids) => { const el = textStyle(h('div', 'ua-title'), Object.assign({ size: 'lg' }, p)); el.append(...kids(true)); return el; },
    text: (p, kids) => { const el = textStyle(h('div', 'ua-text'), p); el.append(...kids(true)); return el; },
    caption: (p, kids) => { const el = textStyle(h('div', 'ua-caption'), p); el.append(...kids(true)); return el; },
    label: (p, kids) => { const el = textStyle(h('label', 'ua-label'), p); el.append(...kids(true)); return el; },
    span: (p, kids) => { const el = textStyle(h('span'), p); el.append(...kids(true)); return el; },
    underline: (p, kids) => { const el = h('u'); el.append(...kids(true)); return el; },
    blockquote: (p, kids) => { const el = h('blockquote', 'ua-quote'); el.append(...kids()); return el; },
    badge: (p, kids) => {
      const el = h('span', 'ua-badge');
      const c = p.color || 'secondary';
      el.style.color = color(c);
      el.style.background = `color-mix(in srgb, ${color(c)} 16%, transparent)`;
      el.append(...kids(true)); return el;
    },
    kbd: (p, kids) => { const el = h('kbd', 'ua-kbd'); el.append(...kids(true)); return el; },
    icon: (p) => icon(p),
    image: (p) => COMPONENTS.AsyncImage(p),
    img: (p) => COMPONENTS.AsyncImage(p),
    AsyncImage: (p) => {
      const src = p.src || p.url || '';
      if (!src) {  // a search query with no image found yet: a quiet placeholder
        const empty = boxStyle(h('div', 'ua-image ua-image-empty'), Object.assign({ aspectRatio: '16:9', radius: 'lg' }, p, { onClick: undefined }));
        empty.textContent = p.alt || p.query || '';
        return empty;
      }
      const el = h('img', 'ua-image');
      boxStyle(el, Object.assign({}, p, { onClick: undefined }));
      el.src = src;
      el.alt = p.alt || p.query || '';
      if (p.aspectRatio) el.style.objectFit = p.fit || 'cover';
      if (p.onClick) on(el, 'click', p.onClick);
      return el;
    },
    button: (p, kids) => {
      const el = h('button', `ua-button ua-${p.variant || 'solid'} ua-${p.size || 'md'}`);
      el.type = p.submit ? 'submit' : 'button';
      el.style.setProperty('--ua-btn', color(p.color || 'primary'));
      if (p.block) el.style.width = '100%';
      if (p.disabled) el.disabled = true;
      if (p.onClick) on(el, 'click', () => p.onClick());
      el.append(...kids(true)); return el;
    },
    link: (p, kids) => {
      const el = h('a', 'ua-link');
      el.href = p.href || '#';
      on(el, 'click', (e) => { e.preventDefault(); if (p.onClick) p.onClick(); else if (p.href) GenUI.openUrl(p.href); });
      el.append(...kids(true)); return el;
    },
    input: (p) => {
      const el = h('input', 'ua-input');
      el.type = p.type || 'text';
      if (p.placeholder) el.placeholder = p.placeholder;
      for (const key of ['min', 'max', 'step']) if (p[key] !== undefined) el[key] = p[key];
      el.value = p.value === undefined || p.value === null ? '' : p.value;
      if (p.disabled) el.disabled = true;
      if (p.block !== false) el.style.width = '100%';
      on(el, 'input', (e, live) => p.onChange && p.onChange(live.type === 'number' ? (live.value === '' ? '' : Number(live.value)) : live.value));
      if (p.onSubmit) on(el, 'keydown', (e, live) => { if (e.key === 'Enter') { e.preventDefault(); p.onSubmit(live.value); } });
      return el;
    },
    textarea: (p) => {
      const el = h('textarea', 'ua-input');
      el.rows = p.rows || 3;
      if (p.placeholder) el.placeholder = p.placeholder;
      el.value = p.value || '';
      on(el, 'input', (e, live) => {
        if (p.autoResize) { live.style.height = 'auto'; live.style.height = live.scrollHeight + 'px'; }
        if (p.onChange) p.onChange(live.value);
      });
      return el;
    },
    select: (p) => {
      const el = h('select', 'ua-input');
      if (p.block) el.style.width = '100%';
      for (const o of options(p.options)) {
        const opt = h('option'); opt.value = String(o.value); opt.textContent = o.label; el.appendChild(opt);
      }
      el.value = String(p.value);
      on(el, 'change', (e, live) => {
        const picked = options(p.options).find((o) => String(o.value) === live.value);
        if (p.onChange) p.onChange(picked ? picked.value : live.value);
      });
      return el;
    },
    slider: (p) => {
      const el = h('input', 'ua-slider');
      el.type = 'range';
      el.min = p.min ?? 0; el.max = p.max ?? 100; el.step = p.step ?? 1;
      el.value = p.value ?? 0;
      if (p.disabled) el.disabled = true;
      on(el, 'input', (e, live) => p.onChange && p.onChange(Number(live.value)));
      return el;
    },
    checkbox: (p, kids) => {
      const el = h('label', 'ua-check' + (p.checked && p.lineThrough ? ' ua-done' : ''));
      const box = h('input'); box.type = 'checkbox'; box.checked = !!p.checked;
      on(box, 'change', (e, live) => p.onChange && p.onChange(live.checked));
      const text = h('span'); text.append(...kids(true));
      el.append(box, text); return el;
    },
    switch: (p, kids) => {
      const el = COMPONENTS.checkbox(p, kids);
      el.classList.add('ua-switch'); return el;
    },
    'radio-group': (p, kids, ctx) => {
      const el = h('div', 'ua-radios', { display: 'flex', flexDirection: p.direction === 'row' ? 'row' : 'column', gap: space(p.gap ?? 2) });
      const inner = Object.assign({}, ctx, { radio: { value: p.value, onChange: p.onChange, name: 'ua-radio' + ctx.path } });
      el.append(...kids(false, inner)); return el;
    },
    radio: (p, kids, ctx) => {
      const group = ctx.radio || {};
      const el = h('label', 'ua-check');
      const input = h('input'); input.type = 'radio'; input.name = group.name || 'radio';
      input.checked = String(group.value) === String(p.value);
      on(input, 'change', () => group.onChange && group.onChange(p.value));
      const text = h('span'); text.append(...kids(true));
      el.append(input, text); return el;
    },
    'segmented-control': (p) => {
      const el = h('div', 'ua-segmented');
      if (p.block) el.style.display = 'flex';
      for (const o of options(p.options)) {
        const b = h('button', 'ua-segment' + (String(o.value) === String(p.value) ? ' ua-on' : ''));
        b.type = 'button';
        b.textContent = o.label;
        if (p.block) b.style.flex = '1';
        on(b, 'click', () => p.onChange && p.onChange(o.value));
        el.appendChild(b);
      }
      return el;
    },
    tabs: (p, kids, ctx) => COMPONENTS['segmented-control'](p, kids, ctx),
    'date-picker': (p) => COMPONENTS.input(Object.assign({}, p, { type: 'date', block: p.block })),
    'time-picker': (p) => COMPONENTS.input(Object.assign({}, p, { type: 'time' })),
    form: (p, kids) => {
      const el = boxStyle(h('form', 'ua-form'), p, 'column');
      on(el, 'submit', (e) => { e.preventDefault(); if (p.onSubmit) p.onSubmit(); });
      el.append(...kids()); return el;
    },
    progress: (p) => {
      const el = h('div', 'ua-progress');
      const bar = h('div', '', { width: Math.max(0, Math.min(100, (Number(p.value) || 0) / (p.max || 100) * 100)) + '%', background: color(p.color || 'primary') });
      el.appendChild(bar); return el;
    },
    popover: (p, kids, ctx) => {
      const el = h('div', 'ua-popover');
      const state = ctx.card.local('popover:' + ctx.path, false);
      const inner = Object.assign({}, ctx, { popover: state });
      el.append(...kids(false, inner)); return el;
    },
    'popover-trigger': (p, kids, ctx) => {
      const el = h('div', 'ua-popover-trigger');
      const state = ctx.popover;
      if (state) on(el, 'click', () => state.set(!state.get()));
      el.append(...kids(false)); return el;
    },
    'popover-content': (p, kids, ctx) => {
      const el = h('div', 'ua-popover-content');
      if (!ctx.popover || !ctx.popover.get()) el.hidden = true;
      el.append(...kids()); return el;
    },
    collapsible: (p, kids, ctx) => {
      const el = h('details', 'ua-details');
      if (p.open) el.open = true;
      const summary = h('summary'); summary.textContent = p.title || 'Details';
      el.append(summary, ...kids()); return el;
    },
    carousel: (p, kids, ctx) => {
      const el = h('div', 'ua-carousel');
      const visible = Number(p.visibleItems) || 1;
      const own = ctx.card.local('carousel:' + ctx.path, 0);
      const active = p.activeIndex ?? own.get();
      const items = kids(false).filter((n) => n.nodeType === 1);
      const track = h('div', 'ua-carousel-track', { gap: space(p.gap ?? 2) });
      items.forEach((item, k) => { item.style.flex = `0 0 calc(${100 / visible}% - ${space(p.gap ?? 2)})`; item.hidden = k < active || k >= active + visible; track.appendChild(item); });
      const go = (k) => { const next = Math.max(0, Math.min(items.length - visible, k)); if (p.onChange) p.onChange(next); else own.set(next); };
      el.appendChild(track);
      if (p.showArrows !== false && items.length > visible) {
        const nav = h('div', 'ua-carousel-nav');
        const prev = h('button', 'ua-button ua-ghost ua-sm'); prev.type = 'button'; prev.textContent = '‹'; prev.disabled = active <= 0;
        const next = h('button', 'ua-button ua-ghost ua-sm'); next.type = 'button'; next.textContent = '›'; next.disabled = active >= items.length - visible;
        on(prev, 'click', () => go(active - 1)); on(next, 'click', () => go(active + 1));
        const dots = h('span', 'ua-caption'); dots.textContent = `${active + 1} / ${items.length - visible + 1}`;
        nav.append(prev, dots, next); el.appendChild(nav);
      }
      return el;
    },
    'carousel-item': (p, kids) => { const el = boxStyle(h('div', 'ua-carousel-item'), p, 'column'); el.append(...kids()); return el; },
    table: (p, kids) => { const el = h('table', 'ua-table'); el.append(...kids(false)); return el; },
    'table-row': (p, kids) => {
      const el = h('tr');
      if (p.onClick) { on(el, 'click', p.onClick); el.classList.add('ua-clickable'); }
      el.append(...kids(false).filter((n) => n.nodeType === 1)); return el;
    },
    'table-cell': (p, kids) => {
      const el = h('td');
      if (p.width) el.style.width = size(p.width);
      if (p.align) el.style.textAlign = p.align === 'end' ? 'right' : p.align === 'start' ? 'left' : p.align;
      el.append(...kids(true)); return el;
    },
    Chart: (p) => chart(p.content || p),
    chart: (p) => chart(p.content || p),
    CodeBlock: (p, kids, ctx, node) => {
      const code = dedent(node.raw || '');
      if (p.language === 'mermaid') return mermaid(code);
      const pre = h('pre', 'ua-code');
      const el = h('code');
      el.textContent = code;
      if (p.editable) { el.contentEditable = 'plaintext-only'; el.spellcheck = false; }
      pre.appendChild(el);
      const copy = h('button', 'ua-copy'); copy.type = 'button'; copy.textContent = 'Copy';
      on(copy, 'click', (e, live) => GenUI.copy(live.parentNode.querySelector('code').textContent));
      pre.appendChild(copy);
      return pre;
    },
    video: (p) => {
      // A video file, or a YouTube link played through YouTube's privacy-enhanced embed.
      const src = String(p.src || p.url || '');
      const youtube = /(?:youtube\.com\/(?:watch\?v=|shorts\/|embed\/)|youtu\.be\/)([\w-]{6,})/.exec(src);
      let el;
      if (youtube) {
        el = h('iframe', 'ua-video');
        const start = /[?&]t=(\d+)/.exec(src);
        el.src = `https://www.youtube-nocookie.com/embed/${youtube[1]}?rel=0${p.autoplay ? '&autoplay=1&mute=1' : ''}${start ? '&start=' + start[1] : ''}`;
        el.allow = 'autoplay; encrypted-media; picture-in-picture';
        el.allowFullscreen = true;
      } else {
        el = h('video', 'ua-video');
        if (/^(https?:|blob:|data:video\/)/.test(src) || src.startsWith('/')) el.src = src;
        el.controls = p.controls !== false;
        if (p.autoplay) { el.autoplay = true; el.muted = true; }
        if (p.loop) el.loop = true;
        if (p.poster) el.poster = p.poster;
        el.playsInline = true;
      }
      boxStyle(el, Object.assign({ radius: 'lg', aspectRatio: '16:9' }, p, { onClick: undefined }));
      el.dataset.keep = src;  // a playing video must survive re-renders
      return el;
    },
    Rating: (p) => {
      const value = typeof p.data === 'object' && p.data ? p.data.rating ?? p.data.value : p.value ?? p.data;
      const el = h('span', 'ua-caption');
      el.textContent = value ? '★'.repeat(Math.round(value)) + '☆'.repeat(Math.max(0, 5 - Math.round(value))) + ` ${value}` : '';
      return el;
    },
    Entity: (p, kids) => { const el = h('span'); el.textContent = p.value ?? ''; el.append(...kids(true)); return el; },
    Cite: () => document.createTextNode(''),
  };

  function mermaid(code) {
    const el = h('div', 'ua-mermaid');
    el.textContent = code;
    el.dataset.keep = code;  // drawn asynchronously: re-renders leave it alone
    loadScript(asset('mermaid@11.4.1/dist/mermaid.min.js'), 'mermaid').then((m) => {
      if (!m || !el.isConnected) return;
      m.initialize({ startOnLoad: false, theme: 'dark' });
      m.render('ua-m' + Math.random().toString(36).slice(2), code).then((out) => { el.innerHTML = out.svg; }).catch(() => {});
    });
    return el;
  }

  // Libraries come from the device itself when the host says where (window.GENUI_ASSETS: the PC
  // serves ui/vendor, the phone app ships it); the CDN is only a fallback for a bare browser.
  function asset(path) { return (global.GENUI_ASSETS || 'https://cdn.jsdelivr.net/npm/') + path; }

  const loading = {};
  function loadScript(url, globalName) {
    if (global[globalName]) return Promise.resolve(global[globalName]);
    if (!loading[url]) {
      loading[url] = new Promise((resolve) => {
        const script = document.createElement('script');
        script.src = url;
        script.onload = () => resolve(global[globalName]);
        script.onerror = () => resolve(null);
        document.head.appendChild(script);
      });
    }
    return loading[url];
  }

  function loadStyle(url) {
    if (document.querySelector(`link[href="${url}"]`)) return;
    const link = document.createElement('link');
    link.rel = 'stylesheet'; link.href = url;
    document.head.appendChild(link);
  }

  // ------------------------------------------------------------- rendering

  /** Turns parsed nodes into DOM nodes, mixing Markdown text with components. */
  function renderNodes(nodes, scope, ctx, inline) {
    const parts = [];
    let markdown = '';
    const place = (domNode) => { parts.push(domNode); markdown += PH_OPEN + (parts.length - 1) + PH_CLOSE; };
    const walk = (list, sc, path) => {
      list.forEach((node, k) => {
        const here = path + '.' + k;
        switch (node.t) {
          case 'text': markdown += node.v; break;
          case 'body': runBody(node.code, sc); break;
          case 'expr': {
            const value = guard(() => evaluate(node.code, sc), ctx);
            if (value === undefined || value === null || value === false || value === true) break;
            if (value instanceof Node) place(value);
            else place(document.createTextNode(String(value)));
            break;
          }
          case 'html': {
            const holder = h('span');
            holder.innerHTML = String(guard(() => evaluate(node.code, sc), ctx) ?? '');
            place(holder);
            break;
          }
          case 'math': place(math(node.v, node.display)); break;
          case 'fragment': walk(node.nodes, sc, here); break;
          case 'if': {
            for (const branch of node.branches) {
              if (branch.cond === null || guard(() => evaluate(branch.cond, sc), ctx)) { walk(branch.nodes, sc, here); break; }
            }
            break;
          }
          case 'each': {
            let list = guard(() => evaluate(node.list, sc), ctx) || [];
            if (!Array.isArray(list)) list = typeof list === 'number' ? Array.from({ length: list }, (_, n) => n) : Array.from(list);
            if (!list.length && node.empty) walk(node.empty, sc, here);
            list.forEach((item, n) => {
              const inner = Object.create(sc);
              bindPattern(node.pattern, item, inner);
              if (node.index) inner[node.index] = n;
              walk(node.nodes, inner, here + ':' + n);
            });
            break;
          }
          case 'el': place(renderElement(node, sc, Object.assign({}, ctx, { path: here }))); break;
          default: break;
        }
      });
    };
    walk(nodes, scope, ctx.path || '');
    return fromMarkdown(markdown, parts, inline);
  }

  function guard(fn, ctx) {
    try {
      return fn();
    } catch (error) {
      ctx.card.problem(error);
      return undefined;
    }
  }

  function fromMarkdown(markdown, parts, inline) {
    if (!markdown.trim()) return [];
    const onlyParts = !inline && markdown.replace(PH, '').trim() === '';
    if (onlyParts) return [...markdown.matchAll(PH)].map((m) => parts[Number(m[1])]);
    const holder = document.createElement('div');
    let source = markdown;
    if (inline) source = markdown.replace(/\s*\n\s*/g, ' ').replace(/^\s+|\s+$/g, (m) => (m ? ' ' : ''));
    holder.innerHTML = inline ? inlineMarkdown(source) : blockMarkdown(dedent(markdown));
    // Swap placeholders for the rendered components.
    const walker = document.createTreeWalker(holder, NodeFilter.SHOW_TEXT);
    const texts = [];
    while (walker.nextNode()) if (walker.currentNode.nodeValue.includes(PH_OPEN)) texts.push(walker.currentNode);
    for (const text of texts) {
      const pieces = text.nodeValue.split(PH);
      const frag = document.createDocumentFragment();
      pieces.forEach((piece, k) => {
        if (k % 2) frag.appendChild(parts[Number(piece)]);
        else if (piece) frag.appendChild(document.createTextNode(piece));
      });
      text.parentNode.replaceChild(frag, text);
    }
    // A paragraph that only holds block components is just those components.
    holder.querySelectorAll('p').forEach((p) => {
      const kids = [...p.childNodes].filter((n) => !(n.nodeType === 3 && !n.nodeValue.trim()));
      if (kids.length && kids.every((n) => n.nodeType === 1 && /^(DIV|TABLE|FORM|PRE|HR|BLOCKQUOTE|DETAILS)$/.test(n.tagName))) p.replaceWith(...kids);
    });
    return [...holder.childNodes];
  }

  function math(tex, display) {
    const el = h(display ? 'div' : 'span', 'ua-math' + (display ? ' ua-math-display' : ''));
    el.textContent = tex;
    el.dataset.tex = tex;
    el.dataset.keep = tex;
    if (global.katex) {
      try { global.katex.render(tex, el, { displayMode: display, throwOnError: false }); } catch (e) { /* stays as text */ }
    } else {
      loadStyle(asset('katex@0.16.11/dist/katex.min.css'));
      loadScript(asset('katex@0.16.11/dist/katex.min.js'), 'katex').then((k) => {
        if (k) document.querySelectorAll('.ua-math').forEach((m) => { if (!m.querySelector('.katex')) try { k.render(m.dataset.tex, m, { displayMode: m.classList.contains('ua-math-display'), throwOnError: false }); } catch (e) { /* text */ } });
      });
    }
    return el;
  }

  function props(node, scope, ctx) {
    const out = {};
    for (const attr of node.attrs) {
      if (attr.name === '...') Object.assign(out, guard(() => evaluate(attr.v, scope), ctx) || {});
      else out[attr.name] = attr.kind === 'str' ? attr.v : attr.kind === 'bool' ? true : guard(() => evaluate(attr.v, scope), ctx);
    }
    return out;
  }

  function renderElement(node, scope, ctx) {
    const p = props(node, scope, ctx);
    if (ctx.svg || node.tag === 'svg' || (SVG_TAGS.has(node.tag) && ctx.svg)) return svgElement(node, p, scope, ctx);
    const kids = (inline, innerCtx) => renderNodes(node.nodes, scope, innerCtx || ctx, inline);
    const component = COMPONENTS[node.tag];
    if (component) return component(p, kids, ctx, node);
    // Plain HTML for anything else (div, span, p, b, ul ...), unknown components as a box.
    const tag = /^[a-z][a-z0-9]*$/.test(node.tag) ? node.tag : 'div';
    let el;
    try { el = document.createElement(tag); } catch (e) { el = document.createElement('div'); }
    if (tag === 'div' && node.tag !== 'div') el.dataset.component = node.tag;
    for (const key in p) {
      const value = p[key];
      if (key === 'style' && typeof value === 'object') Object.assign(el.style, value);
      else if (/^on[A-Z]/.test(key) && typeof value === 'function') on(el, key.slice(2).toLowerCase(), value);
      else if (key === 'className' || key === 'class') el.className = value;
      else if (value !== false && value !== undefined && value !== null && typeof value !== 'object' && typeof value !== 'function'
               && !/^on/i.test(key) && !/^(srcdoc|formaction)$/i.test(key) && !(/^(href|src)$/i.test(key) && /^\s*javascript:/i.test(String(value)))) {
        el.setAttribute(key, value === true ? '' : value);
      }
    }
    if (node.raw !== undefined) el.textContent = dedent(node.raw);
    else el.append(...kids(/^(span|b|i|em|strong|a|small|code|label|button|h[1-6]|p|li|td|th)$/.test(tag)));
    return el;
  }

  function svgElement(node, p, scope, ctx) {
    const tag = SVG_TAGS.has(node.tag) ? node.tag : 'g';
    const el = document.createElementNS(SVG_NS, tag);
    for (const key in p) {
      let value = p[key];
      if (value === undefined || value === null || value === false) continue;
      if (/^on[A-Z]/.test(key)) { if (typeof value === 'function') { on(el, key.slice(2).toLowerCase(), value); el.classList.add('ua-clickable'); } continue; }
      if (key === 'style' && typeof value === 'object') { Object.assign(el.style, value); continue; }
      if (typeof value === 'function' || typeof value === 'object') continue;
      const name = key === 'className' ? 'class' : SVG_KEEP_CASE.has(key) || key.includes('-') || key.includes(':') ? key : key.replace(/[A-Z]/g, (m) => '-' + m.toLowerCase());
      if (/^(href|xlink:href)$/.test(name) && /^\s*javascript:/i.test(String(value))) continue;
      el.setAttribute(name, value === true ? '' : value);
    }
    if (tag === 'svg' && !p.width && !p.height) el.setAttribute('width', '100%');
    const inner = Object.assign({}, ctx, { svg: true });
    for (const child of renderSvgChildren(node.nodes, scope, inner)) el.appendChild(child);
    return el;
  }

  function renderSvgChildren(nodes, scope, ctx) {
    const out = [];
    const walk = (list, sc, path) => list.forEach((node, k) => {
      const here = path + '.' + k;
      if (node.t === 'el') out.push(renderElement(node, sc, Object.assign({}, ctx, { path: here })));
      else if (node.t === 'text') { if (node.v.trim()) out.push(document.createTextNode(node.v.replace(/\s+/g, ' '))); }
      else if (node.t === 'expr') { const v = guard(() => evaluate(node.code, sc), ctx); if (v !== undefined && v !== null && v !== false) out.push(document.createTextNode(String(v))); }
      else if (node.t === 'body') runBody(node.code, sc);
      else if (node.t === 'fragment') walk(node.nodes, sc, here);
      else if (node.t === 'if') {
        for (const b of node.branches) if (b.cond === null || guard(() => evaluate(b.cond, sc), ctx)) { walk(b.nodes, sc, here); break; }
      } else if (node.t === 'each') {
        const list = guard(() => evaluate(node.list, sc), ctx) || [];
        Array.from(list).forEach((item, n) => {
          const inner = Object.create(sc);
          bindPattern(node.pattern, item, inner);
          if (node.index) inner[node.index] = n;
          walk(node.nodes, inner, here + ':' + n);
        });
      }
    });
    walk(nodes, scope, ctx.path || '');
    return out;
  }

  // Patch the live DOM to match a fresh render, keeping focus, caret and scroll.
  function morph(from, to) {
    if (from.nodeType !== to.nodeType || from.nodeName !== to.nodeName || from.namespaceURI !== to.namespaceURI) {
      from.replaceWith(to);
      return;
    }
    if (from.nodeType === 3) { if (from.nodeValue !== to.nodeValue) from.nodeValue = to.nodeValue; return; }
    if (from.nodeType !== 1) return;
    if (from.dataset && from.dataset.keep !== undefined && from.dataset.keep === to.dataset.keep) { from.__ua = to.__ua; return; }
    for (const attr of [...from.attributes]) if (!to.hasAttribute(attr.name)) from.removeAttribute(attr.name);
    for (const attr of [...to.attributes]) if (from.getAttribute(attr.name) !== attr.value) from.setAttribute(attr.name, attr.value);
    from.__ua = to.__ua;
    if (from.tagName === 'DETAILS') to.open = from.open;
    if (from.isContentEditable && document.activeElement === from) return;
    const wanted = from.tagName === 'SELECT' ? to.value : null;
    const a = [...from.childNodes], b = [...to.childNodes];
    b.forEach((node, k) => {
      if (k < a.length) morph(a[k], node);
      else from.appendChild(node);
    });
    for (let k = b.length; k < a.length; k++) a[k].remove();
    if (from.tagName === 'INPUT' || from.tagName === 'TEXTAREA' || from.tagName === 'SELECT') {
      const value = wanted ?? to.value;
      if ((document.activeElement !== from || from.type === 'range') && from.value !== value) from.value = value;
      if (from.checked !== to.checked) from.checked = to.checked;
      if (from.disabled !== to.disabled) from.disabled = to.disabled;
    }
  }

  // ------------------------------------------------------------- host APIs

  const GenUI = {
    copy(text) {
      const value = String(text ?? '');
      const fallback = () => {
        const area = document.createElement('textarea');
        area.value = value; document.body.appendChild(area); area.select();
        try { document.execCommand('copy'); } catch (e) { /* nothing more to try */ }
        area.remove();
      };
      if (navigator.clipboard) navigator.clipboard.writeText(value).catch(fallback); else fallback();
      Card.current && Card.current.toast('Copied');
    },
    openUrl(url) {
      if (!/^(https?:|mailto:)/.test(String(url))) return;
      if (Card.current) Card.current.send({ type: 'open_url', url: String(url) });
      global.open(String(url), '_blank', 'noopener');
    },
    openEntityDetail(entity) { Card.current && Card.current.send({ type: 'action', name: 'open_entity', data: entity }); },
  };

  // --------------------------------------------------------------- the card

  class Card {
    constructor(root, element, options) {
      this.root = root;
      this.options = options || {};
      this.hooks = [];
      this.locals = new Map();
      this.problems = [];
      this.pending = false;
      root.classList.add('ua-card-root');
      root.addEventListener('click', (e) => this.dispatch('click', e));
      root.addEventListener('input', (e) => this.dispatch('input', e));
      root.addEventListener('change', (e) => this.dispatch('change', e));
      root.addEventListener('submit', (e) => this.dispatch('submit', e));
      root.addEventListener('keydown', (e) => this.dispatch('keydown', e));
      this.update(element, true);
    }

    update(element, first) {
      const changedMarkup = !this.element || this.element.ui !== element.ui;
      this.element = element;
      if (first && Array.isArray(element.state)) this.hooks = element.state.slice();
      if (changedMarkup && !first) this.locals.clear();
      this.render();
    }

    dispatch(type, event) {
      for (let node = event.target; node && node !== this.root.parentNode; node = node.parentNode) {
        const handler = node.__ua && node.__ua[type];
        if (handler) {
          if (type === 'click' && node.tagName === 'A') event.preventDefault();
          Card.current = this;
          guard(() => handler(event, node), { card: this });
          if (type === 'submit') event.preventDefault();
          return;
        }
        if (type === 'click' && node.tagName === 'A' && node.dataset.url) { event.preventDefault(); GenUI.openUrl(node.dataset.url); return; }
      }
    }

    tick(ms) {
      const every = Math.max(200, Number(ms) || 1000);
      if (!this.ticker || this.tickEvery !== every) {
        clearInterval(this.ticker);
        this.tickEvery = every;
        this.ticker = setInterval(() => (this.root.isConnected ? this.schedule() : clearInterval(this.ticker)), every);
      }
      return Date.now();
    }

    /** Renderer-owned state (popovers, carousels) that survives re-renders. */
    local(key, initial) {
      if (!this.locals.has(key)) this.locals.set(key, initial);
      return { get: () => this.locals.get(key), set: (v) => { this.locals.set(key, v); this.schedule(); } };
    }

    useState(initial) {
      const index = this.hookIndex++;
      if (!(index in this.hooks)) this.hooks[index] = typeof initial === 'function' ? initial() : initial;
      const set = (value) => {
        const next = typeof value === 'function' ? value(this.hooks[index]) : value;
        if (Object.is(next, this.hooks[index])) return;
        this.hooks[index] = next;
        this.schedule();
        this.saveState();
      };
      return [this.hooks[index], set];
    }

    schedule() {
      if (this.pending) return;
      this.pending = true;
      Promise.resolve().then(() => { this.pending = false; this.render(); });
    }

    render() {
      Card.current = this;
      this.hookIndex = 0;
      this.problems = [];
      const card = this;
      const DIL = {
        useState: (v) => card.useState(v),
        useMemo: (fn) => fn(),
        useRef: (v) => card.useState(() => ({ current: v }))[0],
        useEffect: () => {},
        /** The current time, re-rendering every `ms` while the card is shown (clocks, countdowns). */
        useTick: (ms) => card.tick(ms),
      };
      const element = this.element;
      const scope = Object.create(null);
      // The card's data fields are plain names too, but never hide `data` itself or the host APIs.
      Object.assign(scope, element.data || {});
      Object.assign(scope, {
        DIL, GenUI, UA: this.api(), data: element.data || {}, Math, JSON, Number, String, Array, Object, Date,
      });
      let nodes;
      try {
        nodes = renderNodes(parse(element.ui || ''), scope, { card: this, path: '' }, false);
      } catch (error) {
        this.problem(error);
        nodes = [document.createTextNode('This card could not be drawn: ' + error.message)];
      }
      const fresh = document.createElement('div');
      fresh.append(...nodes);
      if (this.problems.length) {
        const note = h('div', 'ua-problem');
        note.textContent = '⚠ ' + this.problems[0];
        fresh.appendChild(note);
      }
      if (this.toastText) { const t = h('div', 'ua-toast'); t.textContent = this.toastText; fresh.appendChild(t); }
      if (!this.root.firstChild) this.root.appendChild(document.createElement('div'));
      morph(this.root.firstChild, fresh);
      if (this.options.onRender) this.options.onRender(this);
    }

    problem(error) {
      const text = String((error && error.message) || error);
      if (!this.problems.includes(text)) this.problems.push(text);
      if (this.problems.length === 1 && this.lastReported !== text) {
        this.lastReported = text;
        this.send({ type: 'error', text });
      }
    }

    toast(text) {
      this.toastText = text;
      this.schedule();
      clearTimeout(this.toastTimer);
      this.toastTimer = setTimeout(() => { this.toastText = ''; this.schedule(); }, 1400);
    }

    api() {
      const card = this;
      return {
        /** Send a message to the assistant as if the user said it (it sees the card's id and state). */
        send: (text) => card.send({ type: 'message', text: String(text ?? '') }),
        /** A named action with data, for the agent that made the card. */
        action: (name, data) => card.send({ type: 'action', name: String(name), data: data ?? null }),
        /** Remove this card from the canvas. */
        close: () => card.send({ type: 'close' }),
        copy: GenUI.copy,
        openUrl: GenUI.openUrl,
        id: card.element.id,
      };
    }

    send(event) {
      const { base, token } = this.options;
      if (this.options.onEvent) this.options.onEvent(Object.assign({ state: this.hooks }, event));
      if (!base) return Promise.resolve(null);
      return fetch(`${base}/api/canvas/${encodeURIComponent(this.element.id)}/events`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', 'x-ultimate-assistant-token': token || '' },
        body: JSON.stringify(Object.assign({ state: this.hooks }, event)),
      }).catch(() => null);
    }

    saveState() {
      const { base, token, onState } = this.options;
      if (!base && !onState) return;
      clearTimeout(this.saveTimer);
      this.saveTimer = setTimeout(() => {
        if (onState) { onState(this.hooks); return; }  // a host app (the phone) saves it itself
        fetch(`${base}/api/canvas/${encodeURIComponent(this.element.id)}/state`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', 'x-ultimate-assistant-token': token || '' },
          body: JSON.stringify({ state: this.hooks }),
        }).catch(() => null);
      }, 400);
    }
  }

  // Icons arrive once Lucide loads; redraw every card then.
  const cards = new Set();
  loadScript(asset('lucide@0.460.0/dist/umd/lucide.min.js'), 'lucide').then((lib) => {
    if (lib) cards.forEach((c) => c.render());
  });

  global.GenUI = GenUI;
  global.GenUICard = {
    mount(root, element, opts) { const card = new Card(root, element, opts); cards.add(card); return card; },
    parse,
    morph,
    markdown: blockMarkdown,
  };
})(window);
