# 墨读 · Markdown 手机阅读器

一个供个人使用的 Markdown 书架。文件上传后保存到服务器，手机再次打开可直接继续阅读。FastAPI 提供接口和静态页面，SQLite 保存会话、阅读进度、偏好和文件信息，原始 Markdown 单独保存。

首版包含文件上传、书架、下载与删除、手机排版、章节目录、浅色/深色主题、字号调整、阅读位置恢复，以及 KaTeX 数学公式和代码高亮。公式库、字体和前端资源随应用提供。

## 本地运行

需要 Python 3.11+、Node.js 24（或满足 Vite 要求的 Node.js 20.19 / 22.12+）和 npm。在仓库根目录执行：

```bash
./scripts/run-local.sh
```

首次运行会创建 `.venv`、安装依赖、构建前端，并在 `.env` 不存在时生成随机登录密码。密码只在首次生成时显示，也保存在本地 `.env` 中。已有 `.env` 不会被覆盖；依赖文件未变化时，后续启动会复用已安装依赖。

打开 [http://127.0.0.1:8000](http://127.0.0.1:8000)。本地脚本仅监听当前电脑；手机使用部署后的 HTTPS 地址访问。按 `Ctrl+C` 停止服务，`data/` 中的数据仍保留。

**没有默认登录密码。** 手动配置时先复制模板，然后填写自己的长随机密码：

```bash
test -f .env || (umask 077; cp .env.example .env)
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

把生成结果填入 `.env` 的 `READER_PASSWORD`。本地 HTTP 使用 `READER_COOKIE_SECURE=false` 和 `READER_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000`；HTTPS 部署使用 `true` 和自己的完整域名地址。修改配置后重启后端。

## 验证

在运行本地脚本、完成依赖安装后执行：

```bash
.venv/bin/python -m pytest
cd frontend
npm run typecheck
npm test
npm run build
```

实际测试结果见 [验证记录](docs/verification.md)。

真实测试文件位于 `tests/fixtures/概率、信息论与学习目标.md`，可以登录后上传，检查中文、矩阵、多行推导和长公式在手机上的显示。默认单文件上限为 5 MiB，只接受 UTF-8 的 `.md` / `.markdown` 文件。

公式支持 `$…$`、`$$…$$`、`\(…\)` 和 `\[…\]`，按 KaTeX 支持的语法渲染，无法解析的公式保留原文并提示。首版不上传图片附件，文件中的本地相对图片不会随 Markdown 一起保存；可改用 HTTPS 图片地址，浏览器会直接请求这些图片。原始 HTML 按文本显示，Mermaid、脚注和需要完整 LaTeX 编译的内容暂不支持。

## Docker 部署

先在云服务器准备 Docker Compose、域名和 HTTPS 反向代理。在仓库根目录创建 `.env`，配置自己的密码、`READER_COOKIE_SECURE=true`、`READER_TRUSTED_ORIGINS=https://自己的域名`，然后执行：

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/api/health
```

应用以非 root 用户运行。Compose 将端口绑定到服务器的 `127.0.0.1:8000`，通过 Nginx 提供公网 HTTPS；数据保存在 `reader_data` 命名卷中，更新镜像或重建容器不会清除文件。**不要使用 `docker compose down -v`，该命令会删除数据卷。**

完整的 [部署、Nginx 和恢复说明](docs/deployment.md) 包含可替换域名的配置示例。该仓库提供部署配置，实际上传到云服务器和上线由你执行。

已有宿主机 Caddy 的服务器可使用 [首次部署脚本](scripts/deploy-caddy.sh)，先按 [Caddy 部署说明](docs/deployment.md#已有宿主机-caddy-的首次部署) 确认配置来源和域名。脚本会检查端口、隔离数据卷、备份配置并校验后重载。

## 备份

```bash
./scripts/backup.sh /你的私人备份目录
```

脚本暂停 Compose 的 `reader` 服务，将 SQLite 和原始文件一起归档，结束后恢复原本运行的服务，确保同一时刻的数据一致。备份仅包含数据卷，不包含 `.env`；会话和私人文件仍属于敏感数据，应保存在非公开目录。恢复步骤见 [部署说明](docs/deployment.md#恢复备份)。

## 配置与目录

| 配置 | 用途 |
| --- | --- |
| `READER_PASSWORD` | 必填的个人登录密码 |
| `READER_BIND_PORT` | Docker 宿主机回环端口，默认 `8000`；可选择空闲端口 |
| `COMPOSE_PROJECT_NAME` | 可选的固定 Compose 项目名；隔离容器、网络和数据卷 |
| `READER_COOKIE_SECURE` | HTTPS 部署为 `true`；本地 HTTP 为 `false` |
| `READER_TRUSTED_ORIGINS` | 反向代理后的完整访问地址，可用英文逗号分隔 |
| `READER_DATA_DIR` | 本地默认 `./data`，Docker 固定为 `/app/data` |
| `READER_STATIC_DIR` | 本地默认 `./frontend/dist`，Docker 固定为 `/app/frontend/dist` |
| `READER_MAX_UPLOAD_BYTES` | 默认 `5242880`，即 5 MiB |

`backend/` 为服务端，`frontend/` 为阅读页面，`tests/` 为后端测试和真实 Markdown 样本。[接口约定](docs/API.md) 记录登录、文件、阅读进度和偏好接口。
