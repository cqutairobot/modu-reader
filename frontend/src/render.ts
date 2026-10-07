import MarkdownIt, { type Token } from 'markdown-it';
import katex from 'katex';
import texmath from 'markdown-it-texmath';
import hljs from 'highlight.js/lib/common';

export interface Heading { id: string; text: string; level: number }
export interface RenderResult {
  html: string;
  headings: Heading[];
  mathCount: number;
  errors: { source: string; message: string }[];
}

/** Parse math before Markdown escapes it; never execute raw HTML or code. */
export function renderMarkdown(source: string): RenderResult {
  const result: RenderResult = { html: '', headings: [], mathCount: 0, errors: [] };
  const headingAliases = new Map<string, string>();
  const slugCounts = new Map<string, number>();
  const md = new MarkdownIt({
    html: false,
    linkify: true,
    typographer: false,
    highlight(code, language) {
      if (language && hljs.getLanguage(language)) {
        try { return hljs.highlight(code, { language, ignoreIllegals: true }).value; }
        catch { /* Fall through to escaped plain text. */ }
      }
      return '';
    },
  });
  md.use(texmath, { engine: katex, delimiters: ['dollars', 'brackets'] });
  const escape = md.utils.escapeHtml;

  // Override texmath's renderers so errors are collected for this document only,
  // and every formula has fresh macros rather than sharing untrusted definitions.
  function math(token: Token, display: boolean): string {
    result.mathCount += 1;
    try {
      return katex.renderToString(token.content, {
        displayMode: display,
        throwOnError: true,
        strict: false,
        trust: false,
        maxSize: 10,
        maxExpand: 500,
        macros: {},
        output: 'htmlAndMathml',
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : '公式无法解析';
      result.errors.push({ source: token.content, message });
      return `<span class="math-error" title="${escape(message)}"><span class="math-error-label">公式未能渲染</span><code>${escape(token.content)}</code></span>`;
    }
  }

  for (const name of ['math_inline', 'math_inline_double']) {
    md.renderer.rules[name] = (tokens, index) =>
      `<span class="math-inline">${math(tokens[index], name === 'math_inline_double')}</span>`;
  }
  for (const name of ['math_block', 'math_block_eqno']) {
    md.renderer.rules[name] = (tokens, index, options, env, renderer) => {
      const token = tokens[index];
      const number = name === 'math_block_eqno' ? `<span class="math-number">(${escape(token.info)})</span>` : '';
      return `<div class="math-block"${renderer.renderAttrs(token)} tabindex="0" role="region" aria-label="数学公式">${math(token, true)}${number}</div>\n`;
    };
  }

  for (const name of ['fence', 'code_block']) {
    const original = md.renderer.rules[name]!;
    md.renderer.rules[name] = (tokens, index, options, env, renderer) => {
      const token = tokens[index];
      const wrapperAttributes = renderer.renderAttrs(token);
      const attributes = token.attrs;
      // The built-in renderer also renders attrs on <pre>/<code>. Keep the
      // resume anchor on the wrapper only, otherwise the DOM has duplicate IDs.
      token.attrs = attributes?.filter(([name]) => name !== 'id' && name !== 'data-reader-block') ?? null;
      try {
        return `<div class="code-block"${wrapperAttributes}>${original(tokens, index, options, env, renderer)}</div>\n`;
      } finally {
        token.attrs = attributes;
      }
    };
  }
  md.renderer.rules.table_open = (tokens, index, options, env, renderer) =>
    `<div class="table-scroll"${renderer.renderAttrs(tokens[index])} tabindex="0" role="region" aria-label="表格"><table>\n`;
  md.renderer.rules.table_close = () => '</table></div>\n';

  const image = md.renderer.rules.image!;
  md.renderer.rules.image = (tokens, index, options, env, renderer) => {
    tokens[index].attrSet('loading', 'lazy');
    tokens[index].attrSet('decoding', 'async');
    tokens[index].attrSet('referrerpolicy', 'no-referrer');
    return image(tokens, index, options, env, renderer);
  };
  const link = md.renderer.rules.link_open;
  md.renderer.rules.link_open = (tokens, index, options, env, renderer) => {
    const token = tokens[index];
    const href = String(token.attrGet('href') ?? '');
    if (href.startsWith('#')) {
      try {
        const anchor = headingAliases.get(decodeURIComponent(href.slice(1)));
        if (anchor) token.attrSet('href', `#${anchor}`);
      } catch { /* Preserve malformed fragments as harmless text links. */ }
    }
    if (/^https?:\/\//i.test(href)) {
      token.attrSet('target', '_blank');
      token.attrSet('rel', 'noopener noreferrer');
    }
    return link ? link(tokens, index, options, env, renderer) : renderer.renderToken(tokens, index, options);
  };

  const tokens = md.parse(source.replace(/^\uFEFF/, ''), {});
  let blockIndex = 0;
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index];
    if (token.type === 'heading_open') {
      const id = `heading-${result.headings.length}`;
      const inline = tokens[index + 1];
      const text = inline?.children?.map(child => child.content).join('') ?? inline?.content ?? '';
      token.attrSet('id', id);
      token.attrSet('data-reader-block', '');
      result.headings.push({ id, text, level: Number(token.tag.slice(1)) });
      const baseSlug = text.trim().toLowerCase().replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, '').replace(/\s/g, '-');
      const count = slugCounts.get(baseSlug) ?? 0;
      slugCounts.set(baseSlug, count + 1);
      headingAliases.set(count ? `${baseSlug}-${count}` : baseSlug, id);
      headingAliases.set(id, id);
    } else if (token.block && token.map && token.level === 0 && token.nesting !== -1 && token.type !== 'inline' && !token.hidden) {
      token.attrSet('id', `block-${blockIndex++}`);
      token.attrSet('data-reader-block', '');
    }
  }
  result.html = md.renderer.render(tokens, md.options, {});
  return result;
}
