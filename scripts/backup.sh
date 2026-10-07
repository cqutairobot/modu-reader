#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
umask 077
command -v docker >/dev/null || { echo "需要 Docker 和 Docker Compose。" >&2; exit 1; }
docker compose version >/dev/null

BACKUP_DIR="${1:-$PROJECT_ROOT/backups}"
mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
case "$BACKUP_DIR/" in
  "$PROJECT_ROOT/frontend/"* | "$PROJECT_ROOT/data/"*)
    echo "请选择前端目录和数据目录之外的私人备份目录。" >&2
    exit 1
    ;;
esac
BACKUP_PATH="$BACKUP_DIR/reader-data-$(date -u +%Y%m%dT%H%M%SZ)-$$.tar.gz"
BACKUP_TEMP="$(mktemp "$BACKUP_DIR/.reader-backup.XXXXXX")"
READER_WAS_RUNNING=false
if [[ -n "$(docker compose ps --status running -q reader)" ]]; then
  READER_WAS_RUNNING=true
fi

cleanup() {
  EXIT_STATUS=$?
  rm -f "$BACKUP_TEMP"
  if [[ "$READER_WAS_RUNNING" == true ]]; then
    if ! docker compose start reader; then
      echo "备份结束后服务未能恢复，请运行 docker compose start reader。" >&2
      EXIT_STATUS=1
    fi
  fi
  exit "$EXIT_STATUS"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

printf '暂停 reader 服务，备份期间请勿让其他进程写入该数据卷。\n'
docker compose stop reader
# 只归档 data，不包含 .env、源代码或登录密码；停写期间 SQLite 和原始文件属于同一份快照。
docker compose run --rm --no-deps -T --entrypoint tar reader -czf - -C /app data > "$BACKUP_TEMP"
tar -tzf "$BACKUP_TEMP" >/dev/null
chmod 600 "$BACKUP_TEMP"
mv "$BACKUP_TEMP" "$BACKUP_PATH"
printf '备份完成：%s\n备份包含私人文件与会话数据，请保存在非公开目录。\n' "$BACKUP_PATH"
