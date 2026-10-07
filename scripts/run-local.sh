#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$PROJECT_ROOT"
umask 077

command -v python3 >/dev/null || { echo "需要 Python 3.11 或更新版本。" >&2; exit 1; }
command -v node >/dev/null || { echo "需要 Node.js 22.12 或更新版本（推荐 Node.js 24）。" >&2; exit 1; }
command -v npm >/dev/null || { echo "需要 npm。" >&2; exit 1; }
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else "需要 Python 3.11 或更新版本。")'
node -e 'const [a,b] = process.versions.node.split(".").map(Number); if (!(a > 22 || (a === 22 && b >= 12) || (a === 20 && b >= 19))) { console.error("需要 Node.js 20.19、22.12 或更新版本；推荐 24。"); process.exit(1); }'

if [[ ! -f .env ]]; then
  LOCAL_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')"
  {
    printf 'READER_PASSWORD=%s\n' "$LOCAL_PASSWORD"
    printf '%s\n' 'READER_COOKIE_SECURE=false' 'READER_TRUSTED_ORIGINS=http://127.0.0.1:8000,http://localhost:8000' 'READER_MAX_UPLOAD_BYTES=5242880' 'READER_DATA_DIR=./data' 'READER_STATIC_DIR=./frontend/dist'
  } > .env
  chmod 600 .env
  printf '\n已创建本地配置 .env。首次登录密码：%s\n请保存此密码，之后也可在 .env 中查看或修改。\n\n' "$LOCAL_PASSWORD"
  unset LOCAL_PASSWORD
fi

if [[ ! -x .venv/bin/python ]]; then
  python3 -m venv .venv
fi
PY_REQUIREMENTS_HASH="$(python3 -c 'import hashlib,pathlib; h=hashlib.sha256(); [h.update(pathlib.Path(p).read_bytes()) for p in ("requirements.txt","requirements-dev.txt")]; print(h.hexdigest())')"
if [[ ! -f .venv/.reader-requirements-hash ]] || [[ "$(cat .venv/.reader-requirements-hash)" != "$PY_REQUIREMENTS_HASH" ]]; then
  .venv/bin/python -m pip install -r requirements-dev.txt
  printf '%s\n' "$PY_REQUIREMENTS_HASH" > .venv/.reader-requirements-hash
fi

NODE_LOCK_HASH="$(python3 -c 'import hashlib,pathlib; print(hashlib.sha256(pathlib.Path("frontend/package-lock.json").read_bytes()).hexdigest())')"
if [[ ! -d frontend/node_modules ]] || [[ ! -f frontend/node_modules/.reader-lock-hash ]] || [[ "$(cat frontend/node_modules/.reader-lock-hash)" != "$NODE_LOCK_HASH" ]]; then
  (cd frontend && npm ci)
  printf '%s\n' "$NODE_LOCK_HASH" > frontend/node_modules/.reader-lock-hash
fi
(cd frontend && npm run build)
printf '\n墨读已准备好，访问 http://127.0.0.1:8000。按 Ctrl+C 停止服务。\n'
exec .venv/bin/python -m uvicorn backend.app:app --env-file .env --host 127.0.0.1 --port 8000
