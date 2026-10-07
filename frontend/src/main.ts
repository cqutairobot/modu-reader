import './styles.css';
import 'katex/dist/katex.min.css';
import { icon } from './icons';

interface Progress { anchor: string | null; offset: number; percent: number; updated_at: string | null }
interface ReaderDocument { id: string; filename: string; title: string; size_bytes: number; created_at: string; last_opened_at: string | null; progress: Progress }
interface OpenDocument extends ReaderDocument { content: string }
interface Preferences { font_size: number; theme: 'light' | 'dark' | 'system' }
interface Session { authenticated: boolean; csrf_token: string | null; max_upload_bytes: number }
interface Heading { id: string; text: string; level: number }
class ApiError extends Error { constructor(message: string, readonly status: number) { super(message); } }

const app = document.querySelector<HTMLDivElement>('#app')!;
const toasts = document.querySelector<HTMLDivElement>('#toast-region')!;
const systemTheme = window.matchMedia('(prefers-color-scheme: dark)');
const progressClientId = typeof crypto.randomUUID === 'function' ? crypto.randomUUID() : [...crypto.getRandomValues(new Uint8Array(16))].map(byte => byte.toString(16).padStart(2, '0')).join('');
let progressSequence = 0;
let session: Session = { authenticated: false, csrf_token: null, max_upload_bytes: 5 * 1024 * 1024 };
let preferences: Preferences = readLocalPreferences();
let documents: ReaderDocument[] = [];
let activeDocument: OpenDocument | null = null;
let headings: Heading[] = [];
let blocks: HTMLElement[] = [];
let view: 'login' | 'library' | 'reader' | 'loading' = 'loading';
let navigationVersion = 0;
let uploading = false;
let uploadLabel = '';
let progressTimer: ReturnType<typeof setTimeout> | undefined;
let preferenceTimer: ReturnType<typeof setTimeout> | undefined;
let progressDirty = false;
let restoring = false;
let pendingRestoreProgress: Progress | null = null;
const dialogReadingPositions = new Map<string, { documentId: string; progress: Progress }>();
let restoreVersion = 0;
let scrollFrame = 0;
let preferenceSaveTask: Promise<void> | null = null;
let preferenceDirty = false;
let bootstrapping: Promise<void> | null = null;
const progressQueues = new Map<string, Promise<unknown>>();

function escape(value: string): string { return value.replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[char]!); }
function clamp(value: number, min: number, max: number): number { return Math.min(max, Math.max(min, value)); }
function readLocalPreferences(): Preferences {
  try {
    const stored = JSON.parse(localStorage.getItem('modu-preferences') || '{}');
    return { font_size: clamp(Number(stored.font_size) || 18, 16, 24), theme: ['light', 'dark', 'system'].includes(stored.theme) ? stored.theme : 'system' };
  } catch { return { font_size: 18, theme: 'system' }; }
}
function applyPreferences(): void {
  const dark = preferences.theme === 'dark' || (preferences.theme === 'system' && systemTheme.matches);
  document.documentElement.dataset.theme = dark ? 'dark' : 'light';
  document.documentElement.style.setProperty('--reading-size', `${preferences.font_size}px`);
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', dark ? '#181d1a' : '#f5f4ef');
  try { localStorage.setItem('modu-preferences', JSON.stringify(preferences)); } catch { /* Reading remains available without browser storage. */ }
}
applyPreferences();
systemTheme.addEventListener('change', applyPreferences);

async function api<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const method = init.method?.toUpperCase() || 'GET';
  if (!['GET', 'HEAD', 'OPTIONS'].includes(method) && session.csrf_token && path !== '/api/login') headers.set('X-CSRF-Token', session.csrf_token);
  let response: Response;
  try { response = await fetch(path, { ...init, headers, credentials: 'same-origin' }); }
  catch { throw new ApiError('连接暂时中断，请检查网络后重试。', 0); }
  if (!response.ok) {
    let message = '操作没有完成，请稍后重试。';
    try { const body = await response.json(); if (typeof body.detail === 'string') message = body.detail; } catch { /* Use readable default for proxy errors. */ }
    if (response.status === 401 && path !== '/api/login' && session.authenticated) expireSession();
    throw new ApiError(message, response.status);
  }
  return response.status === 204 ? undefined as T : response.json();
}
function jsonInit(method: string, body: unknown, keepalive = false): RequestInit { return { method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body), keepalive }; }
function toast(message: string, kind: 'success' | 'error' = 'success'): void {
  const node = document.createElement('div');
  node.className = `toast toast-${kind}`;
  node.innerHTML = `${icon(kind === 'error' ? 'alert' : 'check')}<span>${escape(message)}</span><button class="icon-button" aria-label="关闭提示">${icon('close')}</button>`;
  node.querySelector('button')!.addEventListener('click', () => node.remove());
  toasts.append(node);
  setTimeout(() => node.remove(), kind === 'error' ? 9000 : 4500);
}
function errorMessage(error: unknown): string { return error instanceof Error ? error.message : '操作没有完成，请重试。'; }
function showError(error: unknown): void { if (!(error instanceof ApiError && error.status === 401)) toast(errorMessage(error), 'error'); }
function brand(): string { return `<span class="brand-mark">${icon('book')}</span><span class="brand-name">墨读<span class="brand-sub">M O D U</span></span>`; }
function formatSize(bytes: number): string { return bytes < 1024 * 1024 ? `${Math.max(1, Math.round(bytes / 1024))} KB` : `${(bytes / 1024 / 1024).toFixed(1)} MB`; }
function formatDate(value: string): string { const date = new Date(value); return Number.isNaN(date.getTime()) ? '' : date.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric' }); }
function progressText(document: ReaderDocument): string { return document.progress.percent >= 99.5 ? '已读完' : document.progress.percent < 1 ? '未开始阅读' : `已读 ${Math.round(document.progress.percent)}%`; }
function titleFor(document: ReaderDocument): string { return document.title || document.filename.replace(/\.(md|markdown)$/i, ''); }
function routeDocumentId(): string | null {
  const match = location.hash.match(/^#\/document\/([^/]+)$/);
  if (!match) return null;
  try { return decodeURIComponent(match[1]); } catch { return null; }
}
function expireSession(): void {
  session = { ...session, authenticated: false, csrf_token: null };
  navigationVersion += 1;
  activeDocument = null;
  pendingRestoreProgress = null;
  blocks = [];
  clearTimeout(progressTimer);
  progressDirty = false;
  showLogin('登录已过期，请重新输入访问密码。');
}

function showLogin(message = ''): void {
  view = 'login';
  document.title = '登录 · 墨读';
  app.innerHTML = `<div class="login-page"><header class="login-brand">${brand()}</header><main id="main-content" class="login-main"><div class="login-intro"><span class="eyebrow">YOUR PRIVATE READING SPACE</span><h1>让知识，<br />慢慢沉淀。</h1><p>属于你的 Markdown 书架。<br />上传一次，随时回来继续读。</p><div class="login-note">${icon('book')}<span>专注文字，也照顾每一个公式。</span></div></div><section class="login-panel" aria-labelledby="login-title"><div class="panel-symbol">${icon('lock')}</div><h2 id="login-title">打开你的书架</h2><p class="muted">输入服务器设置的个人访问密码</p><form id="login-form"><label for="password">访问密码</label><input id="password" name="password" type="password" autocomplete="current-password" required placeholder="输入访问密码" /><p id="login-error" class="form-error" role="alert">${escape(message)}</p><button class="button button-primary login-submit" type="submit"><span>进入书架</span>${icon('arrow')}</button></form><p class="login-privacy">${icon('lock')}文件保存在你的服务器上</p></section></main><footer class="login-footer">一页一页，读懂自己的世界。</footer></div>`;
  window.scrollTo(0, 0);
  document.querySelector<HTMLFormElement>('#login-form')!.addEventListener('submit', async event => {
    event.preventDefault();
    const form = event.currentTarget as HTMLFormElement;
    const input = form.querySelector<HTMLInputElement>('#password')!;
    const button = form.querySelector<HTMLButtonElement>('button')!;
    const error = form.querySelector<HTMLParagraphElement>('#login-error')!;
    button.disabled = true; button.innerHTML = '<span>正在打开…</span>'; error.textContent = '';
    try {
      session = await api<Session>('/api/login', jsonInit('POST', { password: input.value }));
      input.value = '';
      await bootstrapLibrary();
    } catch (cause) { error.textContent = cause instanceof ApiError && cause.status === 401 ? '密码不正确，请再试一次。' : errorMessage(cause); input.focus(); }
    finally { button.disabled = false; button.innerHTML = `<span>进入书架</span>${icon('arrow')}`; }
  });
}

async function bootstrapLibrary(): Promise<void> {
  if (bootstrapping) return bootstrapping;
  bootstrapping = (async () => {
    const [library, saved] = await Promise.all([api<{ documents: ReaderDocument[] }>('/api/documents'), api<Preferences>('/api/preferences')]);
    if (!session.authenticated) return;
    documents = library.documents;
    preferences = { font_size: clamp(saved.font_size, 16, 24), theme: saved.theme };
    applyPreferences();
    await handleRoute();
  })();
  try { await bootstrapping; } finally { bootstrapping = null; }
}

function libraryHeader(): string {
  return `<header class="library-header"><a class="brand" href="#/" aria-label="墨读首页">${brand()}</a><div class="header-right"><span class="private-label">${icon('lock')}私人书架</span><button class="icon-button" id="library-settings" aria-label="阅读设置" title="阅读设置">${icon('settings')}</button><button class="icon-button" id="logout" aria-label="退出登录" title="退出登录">${icon('logout')}</button></div></header>`;
}
function loadingHeader(): string { return `<header class="library-header"><a class="brand" href="#/" aria-label="墨读首页">${brand()}</a><a class="text-link" href="#/">返回书架 ${icon('back')}</a></header>`; }
function documentCard(doc: ReaderDocument): string {
  const title = titleFor(doc);
  const percent = clamp(doc.progress.percent, 0, 100);
  return `<article class="document-card"><button class="document-open" data-open="${escape(doc.id)}" aria-label="阅读 ${escape(title)}"><div class="document-cover"><span class="document-cover-mark">${icon('file')}</span><span class="file-type">MD</span></div><h3>${escape(title)}</h3><p class="document-filename" title="${escape(doc.filename)}">${escape(doc.filename)}</p><div class="document-meta"><span>${formatSize(doc.size_bytes)}</span><span>${formatDate(doc.created_at)} 上传</span></div></button><div class="document-card-bottom"><div class="document-progress"><span class="${percent >= 99.5 ? 'complete-text' : ''}">${progressText(doc)}</span><div class="mini-progress" aria-hidden="true"><span style="width:${percent}%"></span></div></div><div class="card-actions"><a class="icon-button" href="/api/documents/${encodeURIComponent(doc.id)}/download" aria-label="下载 ${escape(title)}" title="下载原文件">${icon('download')}</a><button class="icon-button" data-delete="${escape(doc.id)}" aria-label="删除 ${escape(title)}" title="删除">${icon('trash')}</button></div></div></article>`;
}
function showLibrary(): void {
  navigationVersion += 1;
  clearTimeout(progressTimer);
  activeDocument = null;
  headings = []; blocks = []; progressDirty = false;
  pendingRestoreProgress = null;
  view = 'library';
  document.title = '我的书架 · 墨读';
  const sorted = [...documents].sort((a, b) => (b.last_opened_at || b.created_at).localeCompare(a.last_opened_at || a.created_at));
  const recent = sorted.find(doc => doc.last_opened_at && doc.progress.percent > 0 && doc.progress.percent < 99.5);
  app.innerHTML = `${libraryHeader()}<main id="main-content" class="library-main"><section class="library-intro"><div><span class="eyebrow">A LITTLE SPACE FOR BIG IDEAS</span><h1>我的书架<span class="heading-dot">.</span></h1><p>好的文字，值得随时翻开。</p></div><button class="button button-primary upload-trigger" ${uploading ? 'disabled' : ''}>${icon(uploading ? 'clock' : 'plus')}<span>${uploading ? escape(uploadLabel || '正在上传…') : '上传 Markdown'}</span></button></section>${recent ? `<section class="continue-card" aria-labelledby="continue-title"><div class="continue-icon">${icon('book')}</div><div class="continue-info"><span class="eyebrow">接着上次读</span><h2 id="continue-title">${escape(titleFor(recent))}</h2><div class="continue-meta"><span>已读 ${Math.round(recent.progress.percent)}%</span><span class="continue-track"><span style="width:${clamp(recent.progress.percent, 0, 100)}%"></span></span></div></div><button class="button button-outline" data-open="${escape(recent.id)}">继续阅读${icon('arrow')}</button></section>` : ''}<section class="books-section" aria-labelledby="books-title"><div class="section-heading"><h2 id="books-title">全部文档 <span>${documents.length.toString().padStart(2, '0')}</span></h2><span class="section-hint">${icon('lock')}上传后自动保存</span></div>${documents.length ? `<div class="document-grid">${sorted.map(documentCard).join('')}</div>` : `<div class="empty-state"><div class="empty-illustration">${icon('book')}</div><span class="eyebrow">YOUR FIRST PAGE STARTS HERE</span><h2>书架，等你翻开第一页</h2><p>上传一份 Markdown，让文字和公式<br class="mobile-break" />在手机上也清晰好读。</p><button class="button button-primary upload-trigger" ${uploading ? 'disabled' : ''}>${icon('upload')}${uploading ? '正在上传…' : '上传第一份文档'}</button><span class="upload-hint">.md / .markdown · 每份最多 ${formatSize(session.max_upload_bytes)}</span></div>`}</section><footer class="library-footer"><span>${icon('book')}墨读 · 专注于阅读</span><span>文件保存在你的服务器上</span></footer><input id="file-input" type="file" accept=".md,.markdown,text/markdown,text/plain" multiple hidden /></main>${settingsMarkup()}${deleteMarkup()}`;
  window.scrollTo(0, 0);
  document.querySelectorAll<HTMLButtonElement>('[data-open]').forEach(button => button.addEventListener('click', () => { location.hash = `/document/${encodeURIComponent(button.dataset.open!)}`; }));
  document.querySelectorAll<HTMLButtonElement>('[data-delete]').forEach(button => button.addEventListener('click', () => showDeleteDialog(button.dataset.delete!)));
  document.querySelectorAll<HTMLButtonElement>('.upload-trigger').forEach(button => button.addEventListener('click', () => document.querySelector<HTMLInputElement>('#file-input')!.click()));
  document.querySelector<HTMLInputElement>('#file-input')!.addEventListener('change', event => {
    const input = event.target as HTMLInputElement;
    const files = [...(input.files || [])]; input.value = ''; void uploadFiles(files);
  });
  document.querySelector('#library-settings')!.addEventListener('click', () => openSettings());
  document.querySelector('#logout')!.addEventListener('click', () => { void logout(); });
  bindSettings(); bindDialogClosers();
}

async function uploadFiles(files: File[]): Promise<void> {
  if (!files.length || uploading) return;
  uploading = true;
  let succeeded = 0;
  const failures: string[] = [];
  for (let i = 0; i < files.length; i += 1) {
    if (!session.authenticated) break;
    const file = files[i];
    uploadLabel = files.length > 1 ? `上传中 ${i + 1}/${files.length}` : '正在上传…';
    if (view === 'library') {
      document.querySelectorAll<HTMLButtonElement>('.upload-trigger').forEach(button => { button.disabled = true; button.innerHTML = `${icon('clock')}<span>${escape(uploadLabel)}</span>`; });
    }
    if (!/\.(md|markdown)$/i.test(file.name)) { failures.push(`${file.name}：请选择 .md 或 .markdown 文件`); continue; }
    if (file.size > session.max_upload_bytes) { failures.push(`${file.name}：超出 ${formatSize(session.max_upload_bytes)} 限制`); continue; }
    if (file.size === 0) { failures.push(`${file.name}：文件为空`); continue; }
    const body = new FormData(); body.append('file', file);
    try {
      const saved = await api<ReaderDocument>('/api/documents', { method: 'POST', body });
      const existing = documents.findIndex(doc => doc.id === saved.id);
      if (existing >= 0) documents[existing] = saved; else documents.push(saved);
      succeeded += 1;
    } catch (error) { if (error instanceof ApiError && error.status === 401) break; failures.push(`${file.name}：${errorMessage(error)}`); }
  }
  uploading = false; uploadLabel = '';
  if (view === 'library' && session.authenticated) showLibrary();
  if (succeeded) toast(succeeded === 1 ? '文档已保存到书架' : `${succeeded} 份文档已保存到书架`);
  if (failures.length) toast(failures.join('；'), 'error');
}
function deleteMarkup(): string { return `<dialog id="delete-dialog" class="dialog confirm-dialog" aria-labelledby="delete-title"><div class="dialog-header"><span class="dialog-title" id="delete-title">删除这份文档？</span><button class="icon-button" data-close="delete-dialog" aria-label="关闭">${icon('close')}</button></div><p id="delete-filename" class="delete-filename"></p><p class="muted">文档和阅读进度会从服务器移除。你可以先下载原文件留存。</p><p id="delete-error" class="form-error" role="alert"></p><div class="dialog-actions"><button class="button button-outline" data-close="delete-dialog">保留文档</button><button class="button button-danger" id="confirm-delete">确认删除</button></div></dialog>`; }
function showDeleteDialog(id: string): void {
  const doc = documents.find(item => item.id === id); if (!doc) return;
  const dialog = document.querySelector<HTMLDialogElement>('#delete-dialog')!;
  dialog.querySelector('#delete-filename')!.textContent = titleFor(doc);
  dialog.querySelector('#delete-error')!.textContent = '';
  const button = dialog.querySelector<HTMLButtonElement>('#confirm-delete')!;
  button.disabled = false; button.textContent = '确认删除';
  button.onclick = async () => {
    button.disabled = true; button.textContent = '正在删除…';
    try { await api<void>(`/api/documents/${encodeURIComponent(id)}`, { method: 'DELETE' }); documents = documents.filter(item => item.id !== id); dialog.close(); showLibrary(); toast('文档已删除'); }
    catch (error) { if (dialog.isConnected) dialog.querySelector('#delete-error')!.textContent = errorMessage(error); }
    finally { button.disabled = false; button.textContent = '确认删除'; }
  };
  dialog.showModal();
  dialog.querySelector<HTMLButtonElement>('[data-close]')!.focus();
}
async function logout(): Promise<void> {
  try {
    await flushProgress();
    await Promise.all([...progressQueues.values()]);
    clearTimeout(preferenceTimer);
    await savePreferences();
    await api<void>('/api/logout', { method: 'POST' });
    session = { ...session, authenticated: false, csrf_token: null }; documents = []; activeDocument = null; location.hash = '/'; showLogin();
  }
  catch (error) { showError(error); }
}

function tocMarkup(): string {
  return headings.length ? `<nav class="toc-list" aria-label="文章章节">${headings.map(heading => `<button data-heading="${escape(heading.id)}" class="toc-link level-${Math.min(heading.level, 4)}" title="${escape(heading.text)}">${escape(heading.text)}</button>`).join('')}</nav>` : '<p class="toc-empty">这篇文档没有章节标题</p>';
}
async function openDocument(id: string): Promise<void> {
  if (activeDocument?.id === id && view === 'reader') return;
  flushProgress();
  const version = ++navigationVersion;
  view = 'loading';
  app.innerHTML = `${loadingHeader()}<main id="main-content" class="reader-loading"><span class="loading-spinner" aria-hidden="true"></span><p>正在展开文档…</p><a class="text-link" href="#/">返回书架</a></main>`;
  try {
    const [doc, renderer] = await Promise.all([api<OpenDocument>(`/api/documents/${encodeURIComponent(id)}`), import('./render')]);
    if (version !== navigationVersion || !session.authenticated) return;
    const rendered = renderer.renderMarkdown(doc.content);
    activeDocument = doc; headings = rendered.headings;
    const known = documents.findIndex(item => item.id === id);
    if (known >= 0) documents[known] = doc; else documents.push(doc);
    showReader(doc, rendered.html, rendered.errors.length);
    await restoreProgress(doc.progress, true);
    if (version !== navigationVersion) return;
    updateScrollState();
  } catch (error) {
    if (version !== navigationVersion || !session.authenticated) return;
    activeDocument = null;
    if (error instanceof ApiError && error.status === 404) {
      history.replaceState(null, '', '#/'); showLibrary(); toast('这份文档不存在，可能已经被删除。', 'error');
    } else {
      app.innerHTML = `${loadingHeader()}<main id="main-content" class="reader-loading"><span class="error-symbol">${icon('alert')}</span><h1>文档暂时打不开</h1><p>${escape(errorMessage(error))}</p><div class="inline-actions"><button class="button button-primary" id="retry-document">重试</button><a class="button button-outline" href="#/">返回书架</a></div></main>`;
      document.querySelector('#retry-document')!.addEventListener('click', () => { void openDocument(id); });
    }
  }
}
function showReader(doc: OpenDocument, html: string, mathErrors: number): void {
  view = 'reader'; progressDirty = false;
  document.title = `${titleFor(doc)} · 墨读`;
  app.innerHTML = `<header class="reader-header"><a class="reader-back" href="#/" aria-label="返回书架">${icon('back')}<span>书架</span></a><span class="reader-header-divider"></span><span class="reader-document-title" title="${escape(titleFor(doc))}">${escape(titleFor(doc))}</span><div class="reader-tools"><button class="icon-button mobile-toc-trigger" id="open-toc" aria-label="打开章节目录" title="章节目录">${icon('list')}</button><button class="icon-button" id="reader-settings" aria-label="阅读设置" title="阅读设置">${icon('settings')}</button><a class="icon-button" href="/api/documents/${encodeURIComponent(doc.id)}/download" aria-label="下载原始 Markdown 文件" title="下载原文件">${icon('download')}</a></div><div class="top-progress" aria-hidden="true"><span id="top-progress-fill"></span></div></header><div class="reader-layout"><aside class="desktop-toc"><a class="reader-brand" href="#/" aria-label="墨读首页">${brand()}</a><div class="toc-heading"><span>本篇目录</span><span>${headings.length.toString().padStart(2, '0')}</span></div>${tocMarkup()}<div class="toc-bottom">${icon('book')}一页一页，慢慢读。</div></aside><main id="main-content" class="reader-main" tabindex="-1"><div class="article-meta"><span class="article-eyebrow">来自你的书架</span><span>${formatSize(doc.size_bytes)}<span class="meta-dot">·</span>${formatDate(doc.created_at)} 上传</span></div>${!headings.some(heading => heading.level === 1) ? `<h1 class="fallback-title">${escape(titleFor(doc))}</h1>` : ''}${mathErrors ? `<aside class="math-notice">${icon('alert')}<span>${mathErrors} 处公式存在语法问题，已保留原文供检查。</span></aside>` : ''}<article class="article-content" aria-label="${escape(titleFor(doc))}">${html}</article><footer class="article-end"><span class="end-rule"></span>${icon('book')}<p>读到这里，也是一种积累。</p><a class="text-link" href="#/">回到书架 ${icon('arrow')}</a></footer></main></div><div class="reading-status" aria-live="off"><span id="reading-section">开始阅读</span><span id="reading-percent">0%</span></div><dialog id="toc-dialog" class="dialog toc-dialog" aria-labelledby="mobile-toc-title"><div class="dialog-header"><span class="dialog-title" id="mobile-toc-title">本篇目录 <span class="muted">${headings.length}</span></span><button class="icon-button" data-close="toc-dialog" aria-label="关闭目录">${icon('close')}</button></div><p class="toc-document-title">${escape(titleFor(doc))}</p>${tocMarkup()}</dialog>${settingsMarkup()}`;
  blocks = [...document.querySelectorAll<HTMLElement>('.article-content [data-reader-block]')];
  document.querySelector('#reader-settings')!.addEventListener('click', () => openSettings());
  document.querySelector('#open-toc')!.addEventListener('click', () => openReadingDialog('toc-dialog'));
  document.querySelectorAll<HTMLButtonElement>('[data-heading]').forEach(button => button.addEventListener('click', () => navigateHeading(button.dataset.heading!)));
  document.querySelectorAll<HTMLAnchorElement>('.article-content a[href^="#"]').forEach(link => link.addEventListener('click', event => {
    event.preventDefault();
    let id: string;
    try { id = decodeURIComponent(link.getAttribute('href')!.slice(1)); } catch { return; }
    if (document.getElementById(id)) navigateHeading(id);
    else toast('这处章节链接没有找到对应位置。', 'error');
  }));
  bindSettings(); bindDialogClosers();
}
function navigateHeading(id: string): void {
  const target = document.getElementById(id); if (!target) return;
  restoreVersion += 1;
  restoring = false;
  pendingRestoreProgress = null;
  dialogReadingPositions.delete('toc-dialog');
  document.querySelector<HTMLDialogElement>('#toc-dialog')?.close();
  target.setAttribute('tabindex', '-1');
  target.focus({ preventScroll: true });
  window.scrollTo({ top: Math.max(0, target.getBoundingClientRect().top + window.scrollY - readingTop()), behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'auto' : 'smooth' });
}
function readingTop(): number { return (document.querySelector('.reader-header')?.getBoundingClientRect().height || 64) + 26; }
function captureProgress(): Progress | null {
  if (view !== 'reader' || !activeDocument || !blocks.length) return null;
  const top = readingTop();
  let block = blocks[0];
  for (const candidate of blocks) { if (candidate.getBoundingClientRect().top <= top + 1) block = candidate; else break; }
  const rect = block.getBoundingClientRect();
  const maxScroll = Math.max(1, document.documentElement.scrollHeight - window.innerHeight);
  return { anchor: block.id || null, offset: clamp((top - rect.top) / Math.max(rect.height, 1), 0, 1), percent: clamp(window.scrollY / maxScroll * 100, 0, 100), updated_at: new Date().toISOString() };
}
async function waitForStableLayout(): Promise<void> {
  await Promise.race([document.fonts.ready, new Promise(resolve => setTimeout(resolve, 1600))]);
  let previous = -1; let stable = 0;
  for (let i = 0; i < 12 && stable < 3; i += 1) {
    await new Promise(resolve => setTimeout(() => requestAnimationFrame(resolve), 40));
    const height = document.querySelector('.article-content')?.getBoundingClientRect().height || 0;
    stable = Math.abs(height - previous) < 1 ? stable + 1 : 0;
    previous = height;
  }
}
async function restoreProgress(progress: Progress | null, initial = false): Promise<void> {
  if (!progress || !activeDocument) return;
  const version = ++restoreVersion;
  const id = activeDocument.id;
  restoring = true;
  pendingRestoreProgress = { ...progress };
  if (initial) await waitForStableLayout(); else await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  if (version !== restoreVersion || activeDocument?.id !== id || view !== 'reader') { if (version === restoreVersion) { restoring = false; pendingRestoreProgress = null; } return; }
  const target = progress.anchor ? document.getElementById(progress.anchor) : null;
  const max = Math.max(0, document.documentElement.scrollHeight - window.innerHeight);
  const position = progress.percent < 0.3 ? 0 : progress.percent >= 99.5 ? max : target ? target.getBoundingClientRect().top + window.scrollY + target.getBoundingClientRect().height * clamp(progress.offset, 0, 1) - readingTop() : max * progress.percent / 100;
  window.scrollTo({ top: clamp(position, 0, max), behavior: 'instant' });
  await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  if (version === restoreVersion) { restoring = false; pendingRestoreProgress = null; updateScrollState(); }
}
function updateScrollState(): void {
  if (view !== 'reader' || !activeDocument) return;
  const progress = captureProgress(); if (!progress) return;
  document.querySelector<HTMLElement>('#top-progress-fill')!.style.width = `${progress.percent}%`;
  document.querySelector('#reading-percent')!.textContent = `${Math.round(progress.percent)}%`;
  const current = [...headings].reverse().find(heading => { const element = document.getElementById(heading.id); return element && element.getBoundingClientRect().top <= readingTop() + 36; });
  document.querySelector('#reading-section')!.textContent = current?.text || '开始阅读';
  document.querySelectorAll<HTMLElement>('[data-heading]').forEach(button => {
    const selected = button.dataset.heading === current?.id;
    button.classList.toggle('is-current', selected);
    if (selected) button.setAttribute('aria-current', 'location'); else button.removeAttribute('aria-current');
  });
  if (restoring) return;
  activeDocument.progress = progress;
  const libraryDoc = documents.find(doc => doc.id === activeDocument!.id); if (libraryDoc) libraryDoc.progress = progress;
  progressDirty = true;
  if (!progressTimer) progressTimer = setTimeout(() => { progressTimer = undefined; flushProgress(); }, 1600);
}
function flushProgress(keepalive = false): Promise<unknown> {
  clearTimeout(progressTimer); progressTimer = undefined;
  if (!activeDocument || !session.authenticated) return Promise.resolve();
  if (!progressDirty && !(keepalive && progressQueues.has(activeDocument.id))) return Promise.resolve();
  const progress = (restoring ? pendingRestoreProgress : captureProgress()) || activeDocument.progress;
  const id = activeDocument.id;
  activeDocument.progress = progress; progressDirty = false;
  const payload = { anchor: progress.anchor, offset: progress.offset, percent: progress.percent, client_id: progressClientId, sequence: ++progressSequence };
  const send = () => api<Progress>(`/api/documents/${encodeURIComponent(id)}/progress`, jsonInit('PUT', payload, keepalive)).catch(error => {
    if (activeDocument?.id === id && session.authenticated) { progressDirty = true; if (!keepalive) showError(error); }
  });
  if (keepalive) return send();
  const previous = progressQueues.get(id) || Promise.resolve();
  const next = previous.catch(() => undefined).then(send);
  progressQueues.set(id, next);
  void next.finally(() => { if (progressQueues.get(id) === next) progressQueues.delete(id); });
  return next;
}

function settingsMarkup(): string {
  return `<dialog id="settings-dialog" class="dialog settings-dialog" aria-labelledby="settings-title"><div class="dialog-header"><span class="dialog-title" id="settings-title">阅读设置</span><button class="icon-button" data-close="settings-dialog" aria-label="关闭设置">${icon('close')}</button></div><p class="settings-intro">找到适合你的阅读节奏。</p><div class="setting-group"><div class="setting-label"><span>正文字号</span><span class="muted" id="font-size-value">${preferences.font_size} px</span></div><div class="font-control"><button class="icon-button" id="font-minus" aria-label="减小字号" ${preferences.font_size <= 16 ? 'disabled' : ''}>${icon('minus')}</button><span><span class="small-a">A</span><span class="large-a">A</span></span><button class="icon-button" id="font-plus" aria-label="增大字号" ${preferences.font_size >= 24 ? 'disabled' : ''}>${icon('plus')}</button></div></div><div class="setting-group"><div class="setting-label">页面主题</div><div class="theme-control" role="group" aria-label="页面主题">${(['light', 'dark', 'system'] as const).map(theme => `<button class="theme-option ${preferences.theme === theme ? 'selected' : ''}" data-theme-option="${theme}" aria-pressed="${preferences.theme === theme}">${icon(theme === 'light' ? 'sun' : theme === 'dark' ? 'moon' : 'system')}<span>${theme === 'light' ? '浅色' : theme === 'dark' ? '深色' : '跟随系统'}</span></button>`).join('')}</div></div><p class="settings-footnote">设置自动保存，在其他设备上也能继续使用。</p></dialog>`;
}
function openReadingDialog(id: string): void {
  const progress = restoring ? pendingRestoreProgress : captureProgress();
  if (progress && activeDocument) dialogReadingPositions.set(id, { documentId: activeDocument.id, progress: { ...progress } });
  else dialogReadingPositions.delete(id);
  document.querySelector<HTMLDialogElement>(`#${id}`)!.showModal();
  // Focusing a native modal can move the page underneath it. Preserve the
  // position captured before showModal rather than sampling that movement.
  if (progress) void restoreProgress(progress);
}
function openSettings(): void { openReadingDialog('settings-dialog'); }
function bindSettings(): void {
  document.querySelector('#font-minus')?.addEventListener('click', () => { void changePreferences({ font_size: clamp(preferences.font_size - 1, 16, 24) }); });
  document.querySelector('#font-plus')?.addEventListener('click', () => { void changePreferences({ font_size: clamp(preferences.font_size + 1, 16, 24) }); });
  document.querySelectorAll<HTMLButtonElement>('[data-theme-option]').forEach(button => button.addEventListener('click', () => { void changePreferences({ theme: button.dataset.themeOption as Preferences['theme'] }); }));
}
function bindDialogClosers(): void {
  document.querySelectorAll<HTMLButtonElement>('[data-close]').forEach(button => button.addEventListener('click', () => document.getElementById(button.dataset.close!) && (document.getElementById(button.dataset.close!) as HTMLDialogElement).close()));
  document.querySelectorAll<HTMLDialogElement>('dialog').forEach(dialog => {
    dialog.addEventListener('close', () => {
      const saved = dialogReadingPositions.get(dialog.id);
      dialogReadingPositions.delete(dialog.id);
      if (saved && view === 'reader' && activeDocument?.id === saved.documentId) void restoreProgress(saved.progress);
    });
    dialog.addEventListener('click', event => {
      if (event.target !== dialog) return;
      const rect = dialog.getBoundingClientRect();
      if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close();
    });
  });
}
async function changePreferences(next: Partial<Preferences>): Promise<void> {
  const saved = dialogReadingPositions.get('settings-dialog');
  const progress = saved && saved.documentId === activeDocument?.id ? saved.progress : restoring ? pendingRestoreProgress : captureProgress();
  preferences = { ...preferences, ...next };
  applyPreferences();
  document.querySelector('#font-size-value')!.textContent = `${preferences.font_size} px`;
  document.querySelector<HTMLButtonElement>('#font-minus')!.disabled = preferences.font_size <= 16;
  document.querySelector<HTMLButtonElement>('#font-plus')!.disabled = preferences.font_size >= 24;
  document.querySelectorAll<HTMLButtonElement>('[data-theme-option]').forEach(button => { const selected = button.dataset.themeOption === preferences.theme; button.classList.toggle('selected', selected); button.setAttribute('aria-pressed', String(selected)); });
  preferenceDirty = true;
  clearTimeout(preferenceTimer); preferenceTimer = setTimeout(() => { void savePreferences(); }, 400);
  if (progress) await restoreProgress(progress);
}
async function savePreferences(): Promise<void> {
  if (preferenceSaveTask) return preferenceSaveTask;
  if (!session.authenticated) return;
  preferenceSaveTask = (async () => {
    while (preferenceDirty && session.authenticated) {
      preferenceDirty = false;
      try { await api<Preferences>('/api/preferences', jsonInit('PUT', { ...preferences })); }
      catch (error) { preferenceDirty = true; showError(error); break; }
    }
  })();
  try { await preferenceSaveTask; } finally { preferenceSaveTask = null; }
}

async function handleRoute(): Promise<void> {
  if (!session.authenticated) { if (view !== 'login') showLogin(); return; }
  const id = routeDocumentId();
  if (id) await openDocument(id);
  else { flushProgress(); showLibrary(); }
}
window.addEventListener('hashchange', () => { void handleRoute(); });
window.addEventListener('scroll', () => {
  if (scrollFrame) return;
  scrollFrame = requestAnimationFrame(() => { scrollFrame = 0; updateScrollState(); });
}, { passive: true });
window.addEventListener('resize', () => { if (!restoring) updateScrollState(); });
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'hidden') {
    flushProgress(true);
    if (preferenceDirty && session.authenticated) {
      clearTimeout(preferenceTimer); preferenceDirty = false;
      void api('/api/preferences', jsonInit('PUT', preferences, true)).catch(() => { preferenceDirty = true; });
    }
  }
});
window.addEventListener('pagehide', () => flushProgress(true));
document.querySelector<HTMLAnchorElement>('.skip-link')!.addEventListener('click', event => {
  event.preventDefault();
  const main = document.querySelector<HTMLElement>('#main-content');
  if (!main) return;
  if (!main.hasAttribute('tabindex')) main.setAttribute('tabindex', '-1');
  main.focus({ preventScroll: true });
  main.scrollIntoView({ block: 'start', behavior: 'auto' });
});

async function start(): Promise<void> {
  try {
    session = await api<Session>('/api/session');
    if (session.authenticated) await bootstrapLibrary(); else showLogin();
  } catch (error) {
    if (view === 'login') return;
    app.innerHTML = `<main id="main-content" class="boot-screen"><span class="boot-mark" aria-hidden="true">墨</span><h1>暂时无法连接书架</h1><p>${escape(errorMessage(error))}</p><button class="button button-primary" id="retry-start">重新连接</button></main>`;
    document.querySelector('#retry-start')!.addEventListener('click', () => { void start(); });
  }
}
void start();
