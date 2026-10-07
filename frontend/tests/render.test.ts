import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { renderMarkdown } from '../src/render';

test('真实数学长文：392 个公式、59 个标题和所有复杂公式可渲染', () => {
  const source = readFileSync(new URL('../../tests/fixtures/概率、信息论与学习目标.md', import.meta.url), 'utf8');
  const result = renderMarkdown(source);
  assert.equal(result.mathCount, 392);
  assert.equal(result.headings.length, 59);
  assert.deepEqual(result.errors, []);
  assert.equal((result.html.match(/class="math-block"/g) ?? []).length, 175);
  assert.equal((result.html.match(/class="math-inline"/g) ?? []).length, 217);
  assert.equal((result.html.match(/class="code-block"/g) ?? []).length, 3);
  assert.match(result.html, /面包/);
  assert.match(result.html, /data-reader-block/);
  assert.ok(!result.html.includes('katex-error'));
});

test('代码、转义美元符号和普通文本不被当作公式，支持括号分隔符', () => {
  const source = '# 章节\n\n`$x$` 与 \\$20。\n\n```python\n# 不是标题\nx = "$y$"\n```\n\n\\(x_{<t}\\)\n\n\\[\n\\frac{a}{b}\n\\]\n\n| A | B |\n| - | - |\n| 1 | $x$ |';
  const result = renderMarkdown(source);
  assert.equal(result.mathCount, 3);
  assert.equal(result.headings.length, 1);
  assert.deepEqual(result.errors, []);
  assert.match(result.html, /<code>\$x\$<\/code>/);
  assert.match(result.html, /\$20/);
  assert.match(result.html, /class="table-scroll"/);
});

test('禁用原始 HTML、危险链接以及 KaTeX HTML 扩展', () => {
  const result = renderMarkdown('<script>alert(1)</script>\n\n[点击](javascript:alert(1))\n\n$\\htmlClass{bad}{x}$');
  assert.ok(!result.html.includes('<script>'));
  assert.ok(!result.html.includes('href="javascript:'));
  assert.ok(!result.html.includes('class="bad"'));
});

test('单个错误公式不影响后续公式，错误原文被转义', () => {
  const result = renderMarkdown('$\\unknown{<img src=x onerror=alert(1)>}$\n\n$x^2$');
  assert.equal(result.mathCount, 2);
  assert.equal(result.errors.length, 1);
  assert.ok(!result.html.includes('<img src=x'));
  assert.match(result.html, /class="math-error"/);
  assert.match(result.html, /class="katex"/);
  assert.deepEqual(renderMarkdown('$x$').errors, []);
});

test('相同正文的段落和标题锚点稳定', () => {
  const source = '# 重复\n\n第一段\n\n# 重复\n\n第二段';
  const first = renderMarkdown(source);
  const second = renderMarkdown(source);
  assert.deepEqual(first.headings, second.headings);
  assert.deepEqual(first.headings.map(h => h.id), ['heading-0', 'heading-1']);
  assert.equal(first.html, second.html);
  assert.match(first.html, /id="block-0"/);
});

test('代码、表格与正文的续读锚点在 DOM 中只出现一次', () => {
  const result = renderMarkdown('# 章节\n\n```python\nx = 1\n```\n\n    x = 2\n\n| A | B |\n| - | - |\n| 1 | 2 |\n\n正文');
  const ids = [...result.html.matchAll(/ id="((?:block|heading)-\d+)"/g)].map(match => match[1]);
  assert.equal(ids.length, 5);
  assert.equal(new Set(ids).size, ids.length);
  assert.equal((result.html.match(/data-reader-block/g) ?? []).length, ids.length);
});

test('文章内中文标题链接映射到稳定的阅读锚点', () => {
  const result = renderMarkdown('[跳转](#中文-标题)\n\n# 中文 标题\n\n# 中文 标题\n\n[第二节](#中文-标题-1)');
  assert.match(result.html, /href="#heading-0"/);
  assert.match(result.html, /href="#heading-1"/);
});
