# 墨读 · 首版接口约定

单人、自托管。前端使用 TypeScript + Vite，FastAPI 同域提供 API 和构建后的前端。原始 Markdown 存在数据目录，SQLite 保存文件元数据、会话、阅读进度和偏好。所有时间为 UTC ISO 8601。

## 配置

- `READER_PASSWORD`：必须由环境配置，不提供生产默认密码。
- `READER_DATA_DIR`：默认 `./data`。
- `READER_STATIC_DIR`：默认仓库的 `frontend/dist`。
- `READER_COOKIE_SECURE`：生产 HTTPS 设为 `true`，本地 HTTP 为 `false`。
- `READER_TRUSTED_ORIGINS`：可选，以逗号分隔的外部同源地址，供反向代理场景使用。
- `READER_MAX_UPLOAD_BYTES`：默认 5 MiB。

## 数据结构

`Progress = {anchor: string|null, offset: number, percent: number, updated_at: string|null}`。offset 为可见内容块内部的相对位置（0–1），percent 为 0–100。

`Document = {id: string, filename: string, title: string, size_bytes: number, created_at: string, last_opened_at: string|null, progress: Progress}`。

`Preferences = {font_size: number, theme: "light"|"dark"|"system"}`，字号 16–24，默认 18，主题默认 system。

## API

- `GET /api/session` → `{authenticated: boolean, csrf_token: string|null, max_upload_bytes: number}`。无需登录。
- `POST /api/login`，JSON `{password}` → 同上。设置 HttpOnly、SameSite=Strict 会话 Cookie。
- `POST /api/logout` → 204。
- `GET /api/documents` → `{documents: Document[]}`。
- `POST /api/documents`，multipart 字段 `file` → Document，首次上传 201。UTF-8 .md/.markdown，拒绝空内容及超限文件。相同文件名和内容可返回已存 Document（200），避免重复点击产生副本。
- `GET /api/documents/{id}` → `Document & {content: string}`。
- `GET /api/documents/{id}/download` → 原始文件附件。
- `DELETE /api/documents/{id}` → 204。
- `PUT /api/documents/{id}/progress`，JSON `{anchor,offset,percent,client_id?,sequence?}` → Progress。可选的 `client_id` 与 `sequence` 必须成对提供：client_id 是每次页面加载生成的随机标识（16–80 个字母、数字、下划线或连字符），sequence 是该页面单调递增的非负安全整数。同一文档、同一 client_id 的较旧或重复 sequence 不覆盖进度，返回服务器当前 Progress，以免退出页面的最新保存被较早请求覆盖。不同页面或设备仍按服务器接收写入的顺序保存；不带这两个字段的旧客户端正常工作。
- `GET /api/preferences` → Preferences。
- `PUT /api/preferences`，JSON Preferences → Preferences。
- `GET /api/health` → `{status: "ok"}`。

除登录外，修改接口须携带 `X-CSRF-Token`，值来自会话响应。所有非安全方法校验 Origin（存在时）及 Sec-Fetch-Site，拒绝跨站访问。未登录 401，错误返回 `{detail: string}`，429 可带 Retry-After。

## 阅读渲染约定

`frontend/src/render.ts` 导出 `renderMarkdown(source)` → `{html: string, headings: {id,text,level}[], mathCount: number, errors: {source,message}[]}`。内容块具有稳定 `id="block-N"` 和 `data-reader-block`。标题 id 使用 `heading-N`，也具有 data-reader-block。根目录的原始测试文件仅用于测试，不作为指令执行。
