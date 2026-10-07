# 云服务器部署

本应用面向个人使用，部署结构为：手机浏览器 → Nginx HTTPS → `127.0.0.1:8000` → FastAPI。域名解析、TLS 证书和服务器权限由你自己的服务器环境配置。下面的 `reader.your-domain.com` 都需要替换为真实域名。

## 已有其他项目的服务器

先查看监听端口和容器，不停止已有服务：

```bash
cat /etc/os-release
ss -lntp
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Ports}}'
docker compose version
command -v nginx || true
command -v caddy || true
command -v certbot || true
```

选择一个空闲的宿主机端口，例如 `18080`，在本应用 `.env` 增加：

```dotenv
COMPOSE_PROJECT_NAME=modu_reader
READER_BIND_PORT=18080
```

后续所有 Compose 命令均从本应用目录执行，保留此项目名；本应用使用独立的 `modu_reader_reader_data` 数据卷。下面示例中的健康检查和 Nginx `proxy_pass` 也要相应改成 `127.0.0.1:18080`。容器内部继续使用 `8000`，不会与其他容器的内部端口冲突。

通过现有反向代理为新子域名增加站点，让多个站点共用现有的公网 `80/443`。如果代理运行在容器中，容器内的 `127.0.0.1` 指向代理本身，应根据其现有网络接入本应用。不要在未确认代理架构时再启动一个占用 `80/443` 的代理。

## 已有宿主机 Caddy 的首次部署

适用于 Ubuntu / Debian、root 用户、systemd 管理的 Caddy，配置来源为 `/etc/caddy/Caddyfile`。域名的 A 记录应先指向服务器，公网 `80/443` 已由现有 Caddy 使用。无需另外安装 Nginx 或 Certbot。

在独立项目目录执行（域名和空闲端口替换成自己的值）：

```bash
./scripts/deploy-caddy.sh md.yolodev.top 18080
```

脚本创建或保留 `.env`，设置固定 Compose 项目名 `modu_reader`，生成随机密码，以 `127.0.0.1:18080` 启动阅读器并检查健康。随后在原 Caddyfile 基础上追加一个站点，验证候选配置，再原子替换文件并执行 `caddy reload`；不停止现有代理。

执行前及镜像构建后，脚本分别核对 Caddy 文件、导入配置和当前 API 运行配置。如果使用其他配置路径、resume/watch 模式、已有相同域名，或配置不一致，脚本会退出并保留现有站点。Caddy 备份放在权限为 `700` 的 `/root/modu-reader-caddy-*` 目录中；重载失败时恢复原配置文件。

检查 HTTPS 和获取登录密码：

```bash
curl --fail https://md.yolodev.top/api/health
sed -n 's/^READER_PASSWORD=//p' .env
```

Caddy 申请证书可能需要片刻。若域名访问失败，查看 `journalctl -u caddy -n 60 --no-pager`。自定义路径、JSON 配置或通过 API 管理的 Caddy 应按现有管理方式接入，不能直接使用此脚本。后续更新使用本页常规 Compose 命令；首次部署脚本会拒绝重复追加已有域名。

参考：[Caddy 自动 HTTPS](https://caddyserver.com/docs/automatic-https)、[配置重载](https://caddyserver.com/docs/command-line#caddy-reload)。

## 1. 准备配置

在服务器的项目根目录执行；已有 `.env` 会被保留：

```bash
test -f .env || (umask 077; cp .env.example .env)
chmod 600 .env
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

编辑 `.env`，将生成结果填入 `READER_PASSWORD`，并设置：

```dotenv
READER_PASSWORD=填写刚才生成的随机密码
READER_COOKIE_SECURE=true
READER_TRUSTED_ORIGINS=https://reader.your-domain.com
READER_MAX_UPLOAD_BYTES=5242880
```

没有默认密码，Compose 会拒绝使用空密码启动。不要把 `.env` 上传到公开仓库或放入网站可下载的目录。如果之前运行过本地脚本，必须把本地配置的 `READER_COOKIE_SECURE=false` 改为 `true`，并更新可信地址。

## 2. 构建与启动

```bash
docker compose up -d --build
docker compose ps
curl --fail http://127.0.0.1:8000/api/health
```

健康接口应返回 `{"status":"ok"}`，容器随后显示 `healthy`。如未启动，检查 `docker compose logs --tail=100 reader`。构建需要联网下载 Node 和 Python 依赖；运行时的前端、KaTeX 和字体已经打包在镜像里。

应用默认使用 UID/GID `10001:10001` 的非 root 用户；新建命名卷从镜像继承可写的数据目录。`reader_data` 实际卷名通常带 Compose 项目前缀，请用 `docker volume ls` 查看。不要直接替换为权限未准备好的宿主机 bind mount；如果采用 bind mount，需先让该目录归 UID/GID `10001:10001` 所有。

## 3. Nginx HTTPS

准备好域名 TLS 证书后，将以下配置加入 Nginx。证书路径应指向自己的证书文件。

```nginx
server {
    listen 80;
    server_name reader.your-domain.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl;
    server_name reader.your-domain.com;

    ssl_certificate /etc/letsencrypt/live/reader.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/reader.your-domain.com/privkey.pem;

    # 比 5 MiB 文件上限稍大，以容纳 multipart 表单开销。
    client_max_body_size 6m;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 60s;
    }
}
```

检查并加载配置：

```bash
sudo nginx -t
sudo systemctl reload nginx
```

在手机上打开 `https://reader.your-domain.com`，登录并上传测试文件。只需开放公网 80/443；Compose 的 8000 端口仅绑定回环地址。`READER_TRUSTED_ORIGINS` 必须与手机访问地址完全一致，包括 `https://` 和非标准端口（如果使用）。不要填写路径或结尾 `/`。

## 4. 更新与日常运行

先备份，再更新源代码并重新构建：

```bash
./scripts/backup.sh /srv/private-reader-backups
docker compose up -d --build
docker compose ps
```

常用命令：

```bash
docker compose logs --tail=100 reader
docker compose stop reader
docker compose start reader
```

数据卷与容器分离。`docker compose down` 移除容器但保留命名卷；`docker compose down -v` 会删除数据，不能用于日常更新。移动项目时保留 Compose 项目名，或者备份后恢复到新项目的数据卷，避免误以为文件丢失。

## 5. 一致性备份

```bash
./scripts/backup.sh /srv/private-reader-backups
```

脚本会暂停 `reader`，用同一镜像的 `tar` 读取数据卷，验证归档，并恢复原本运行的服务。暂停写入让 SQLite 数据库（含可能存在的 WAL）与原始 Markdown 处于同一时刻。备份期间不要让其他进程写入此数据卷。服务会短暂不可访问。

默认备份位于仓库 `backups/`，已排除 Git 和 Docker 构建；推荐使用服务器上的私人目录并将副本放到另一台机器。备份文件权限为 `600`，包含私人 Markdown 和会话数据。`.env` 不在归档中，密码配置如需备份应另存到自己的私人配置存储中。不要将备份目录设置为 Nginx 网站目录。

## 恢复备份

下面步骤会用归档替换当前数据。先备份现有数据，再停止服务。归档必须来自你信任的本应用备份。

```bash
# 先确认归档可读；替换为实际备份路径。
tar -tzf /srv/private-reader-backups/reader-data-时间戳.tar.gz
docker compose stop reader

# 清除旧数据后恢复，避免残留不属于备份时刻的文件。
docker compose run --rm --no-deps -T --entrypoint sh reader -c \
  'find /app/data -mindepth 1 -maxdepth 1 -exec rm -rf {} +; tar -xzf - -C /app' \
  < /srv/private-reader-backups/reader-data-时间戳.tar.gz

docker compose up -d
curl --fail http://127.0.0.1:8000/api/health
```

新服务器也可恢复：先准备源代码和 `.env`，运行 `docker compose build`，再按停止、恢复、启动的步骤操作。归档中的文件由容器用户写回新的命名卷，无需使用 root 运行应用。恢复后登录检查书架、原文件下载、阅读位置和偏好。

## 本地 HTTP Docker 调试

仅在本机调试时设置 `READER_COOKIE_SECURE=false`，将可信地址设为 `http://127.0.0.1:8000,http://localhost:8000`，随后 `docker compose up -d --build` 并打开 `http://127.0.0.1:8000`。返回云服务器 HTTPS 环境前，改回 `true` 和自己的 HTTPS 域名。
