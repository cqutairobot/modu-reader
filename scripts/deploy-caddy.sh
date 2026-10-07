#!/usr/bin/env bash
# Deploy this reader behind an existing systemd Caddy service.
set -euo pipefail
umask 077

READER_PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
READER_DEPLOY_DOMAIN="${1:-}"
READER_DEPLOY_PORT="${2:-18080}"
READER_CADDY_CONFIG=/etc/caddy/Caddyfile
cd "$READER_PROJECT_ROOT"

fail() { printf '%s\n' "$*" >&2; exit 1; }
[[ $EUID -eq 0 ]] || fail '请以 root 在服务器执行。'
[[ "$READER_DEPLOY_DOMAIN" =~ ^([a-z0-9]([a-z0-9-]*[a-z0-9])?\.)+[a-z0-9]([a-z0-9-]*[a-z0-9])?$ ]] || fail '用法：./scripts/deploy-caddy.sh md.yolodev.top 18080'
[[ "$READER_DEPLOY_PORT" =~ ^[0-9]{1,5}$ ]] || fail '端口必须是整数。'
READER_DEPLOY_PORT=$((10#$READER_DEPLOY_PORT))
(( READER_DEPLOY_PORT >= 1024 && READER_DEPLOY_PORT <= 65535 )) || fail '请选择 1024–65535 的空闲端口。'
for command_name in python3 openssl docker caddy curl systemctl ss; do
    command -v "$command_name" >/dev/null || fail "缺少命令：$command_name"
done
docker compose version >/dev/null
systemctl is-active --quiet caddy || fail 'Caddy 服务未运行。'
READER_CADDY_PID="$(systemctl show caddy -p MainPID --value)"
if ! python3 - "$READER_CADDY_PID" "$READER_CADDY_CONFIG" <<'PY'
from pathlib import Path
import sys
pid = sys.argv[1]
if not pid.isdecimal() or int(pid) <= 0:
    sys.exit('无法确认 Caddy 进程。')
args = Path('/proc/' + pid + '/cmdline').read_bytes().decode().strip('\0').split('\0')
config = None
for index, arg in enumerate(args):
    flag = arg.split('=', 1)[0]
    if flag in {'--resume', '-r', '--watch', '-w'}:
        sys.exit('Caddy 使用 resume/watch 模式；未修改配置。')
    if flag in {'--config', '-c'}:
        config = arg.split('=', 1)[1] if '=' in arg else args[index + 1]
if config != sys.argv[2]:
    sys.exit('Caddy 配置路径不是标准 /etc/caddy/Caddyfile。')
PY
then
    systemctl show caddy -p ExecStart -p ExecReload --no-pager
    fail 'Caddy 未使用标准 /etc/caddy/Caddyfile；未修改配置，请把上面两行输出发回。'
fi
[[ -f "$READER_CADDY_CONFIG" && ! -L "$READER_CADDY_CONFIG" ]] || fail 'Caddyfile 不存在或是符号链接；未修改配置。'
getent ahostsv4 "$READER_DEPLOY_DOMAIN" >/dev/null || fail '域名尚未解析；请先添加 A 记录并等待生效。'

READER_BACKUP_DIR="$(mktemp -d /root/modu-reader-caddy-XXXXXXXX)"
caddy adapt --config "$READER_CADDY_CONFIG" --adapter caddyfile > "$READER_BACKUP_DIR/file.json"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:2019/config/ > "$READER_BACKUP_DIR/live.json"
# Do not replace API-managed changes with an older file from disk.
python3 - "$READER_BACKUP_DIR/file.json" "$READER_BACKUP_DIR/live.json" "$READER_DEPLOY_DOMAIN" <<'PY'
import json, sys
with open(sys.argv[1]) as f:
    disk = json.load(f)
with open(sys.argv[2]) as f:
    live = json.load(f)
if disk != live:
    sys.exit('Caddy 当前运行配置与文件不同；未修改配置。请先确认配置来源。')
def contains_domain(value):
    if isinstance(value, dict):
        return any(contains_domain(v) for v in value.values())
    if isinstance(value, list):
        return any(contains_domain(v) for v in value)
    return value == sys.argv[3]
if contains_domain(disk):
    sys.exit('此域名已在 Caddy 中配置；未重复追加，请先检查已有站点。')
for server in disk.get('apps', {}).get('http', {}).get('servers', {}).values():
    settings = server.get('automatic_https', {})
    if settings.get('disable') or settings.get('disable_certificates'):
        sys.exit('现有 Caddy 禁用了自动 HTTPS；未修改配置，请先确认 TLS 策略。')
PY
cp -a "$READER_CADDY_CONFIG" "$READER_BACKUP_DIR/Caddyfile"

# Shell environment must not override this application's private .env.
unset COMPOSE_PROJECT_NAME COMPOSE_FILE COMPOSE_PROFILES
unset READER_PASSWORD READER_BIND_PORT READER_COOKIE_SECURE READER_TRUSTED_ORIGINS READER_MAX_UPLOAD_BYTES
if [[ ! -e .env ]]; then
    READER_NEW_PASSWORD="$(openssl rand -hex 24)"
    cat > .env <<EOF
COMPOSE_PROJECT_NAME=modu_reader
READER_BIND_PORT=$READER_DEPLOY_PORT
READER_PASSWORD=$READER_NEW_PASSWORD
READER_COOKIE_SECURE=true
READER_TRUSTED_ORIGINS=https://$READER_DEPLOY_DOMAIN
READER_MAX_UPLOAD_BYTES=5242880
EOF
    unset READER_NEW_PASSWORD
fi
chmod 600 .env
python3 - .env "$READER_DEPLOY_DOMAIN" "$READER_DEPLOY_PORT" <<'PY'
import sys
config = {}
with open(sys.argv[1]) as f:
    for line in f:
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            config[key] = value
expected = {'COMPOSE_PROJECT_NAME': 'modu_reader', 'READER_BIND_PORT': sys.argv[3],
            'READER_COOKIE_SECURE': 'true', 'READER_TRUSTED_ORIGINS': 'https://' + sys.argv[2]}
if any(config.get(key) != value for key, value in expected.items()) or not config.get('READER_PASSWORD'):
    sys.exit('已有 .env 与本次部署参数不同；已保留原文件，请先核对设置。')
PY
compose() { docker compose --env-file "$READER_PROJECT_ROOT/.env" -f "$READER_PROJECT_ROOT/compose.yaml" -p modu_reader "$@"; }
if [[ -n "$(ss -H -lnt "sport = :$READER_DEPLOY_PORT")" ]]; then
    READER_EXISTING_PORT="$(compose port reader 8000 2>/dev/null || true)"
    [[ "$READER_EXISTING_PORT" == "127.0.0.1:$READER_DEPLOY_PORT" ]] || fail "端口 $READER_DEPLOY_PORT 已被其他服务占用；未修改现有服务。"
fi
printf '构建并启动阅读器，首次构建需要下载依赖。\n'
compose up -d --build reader
curl --fail --silent --show-error --retry 20 --retry-delay 2 --retry-all-errors "http://127.0.0.1:$READER_DEPLOY_PORT/api/health"
printf '\n'

READER_CANDIDATE="$(mktemp /etc/caddy/.modu-reader-XXXXXXXX.caddyfile)"
trap 'rm -f "$READER_CANDIDATE"' EXIT
cp -a "$READER_CADDY_CONFIG" "$READER_CANDIDATE"
cat >> "$READER_CANDIDATE" <<EOF

$READER_DEPLOY_DOMAIN {
    encode gzip
    reverse_proxy 127.0.0.1:$READER_DEPLOY_PORT
}
EOF
caddy validate --config "$READER_CANDIDATE" --adapter caddyfile
cmp --silent "$READER_CADDY_CONFIG" "$READER_BACKUP_DIR/Caddyfile" || fail '部署期间 Caddyfile 被其他进程修改；未替换文件。'
caddy adapt --config "$READER_CADDY_CONFIG" --adapter caddyfile > "$READER_BACKUP_DIR/current-file.json"
curl --fail --silent --show-error --max-time 10 http://127.0.0.1:2019/config/ > "$READER_BACKUP_DIR/current-live.json"
python3 - "$READER_BACKUP_DIR" <<'PY'
from pathlib import Path
import json, sys
folder = Path(sys.argv[1])
with (folder / 'file.json').open() as f:
    original = json.load(f)
for filename in ['current-file.json', 'current-live.json']:
    with (folder / filename).open() as f:
        if json.load(f) != original:
            sys.exit('构建期间 Caddy 配置发生变化；未替换文件，请重新核对。')
PY
mv -f "$READER_CANDIDATE" "$READER_CADDY_CONFIG"
if ! caddy reload --config "$READER_CADDY_CONFIG" --adapter caddyfile --address 127.0.0.1:2019; then
    cp -a "$READER_BACKUP_DIR/Caddyfile" "$READER_CANDIDATE"
    mv -f "$READER_CANDIDATE" "$READER_CADDY_CONFIG"
    fail 'Caddy 重载失败，已恢复原配置文件；请检查 Caddy 运行状态。'
fi
compose ps
printf '\n部署配置已加载。访问：https://%s\n密码保存在：%s/.env\nCaddy 备份：%s\n证书申请可能需要片刻，请检查域名健康接口。\n' "$READER_DEPLOY_DOMAIN" "$READER_PROJECT_ROOT" "$READER_BACKUP_DIR"
