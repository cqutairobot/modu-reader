import hashlib
import sqlite3
import uuid
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import COOKIE_NAME, Settings, create_app


PASSWORD = "correct horse battery staple"
FIXTURE = Path(__file__).parent / "fixtures" / "概率、信息论与学习目标.md"


@pytest.fixture
def settings(tmp_path):
    return Settings(password=PASSWORD, data_dir=tmp_path / "data", static_dir=tmp_path / "static")


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def login(client):
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200, response.text
    assert response.json()["authenticated"] is True
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def upload(client, headers, content=b"# A document\n\nSome content", filename="test.md"):
    return client.post("/api/documents", files={"file": (filename, content, "text/markdown")}, headers=headers)


def test_authentication_protects_documents_download_and_preferences(client):
    session = client.get("/api/session")
    assert session.json() == {"authenticated": False, "csrf_token": None, "max_upload_bytes": 5 * 1024 * 1024}
    document_id = str(uuid.uuid4())
    for endpoint in ["/api/documents", f"/api/documents/{document_id}", f"/api/documents/{document_id}/download", "/api/preferences"]:
        assert client.get(endpoint).status_code == 401
    assert upload(client, {}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    headers = login(client)
    assert "httponly" in client.post("/api/login", json={"password": PASSWORD}).headers["set-cookie"].lower()
    assert client.get("/api/session").json()["csrf_token"] != headers["X-CSRF-Token"]
    assert client.get("/api/preferences").json() == {"font_size": 18, "theme": "system"}


def test_upload_real_fixture_and_download_preserves_original_bytes(client, settings):
    headers = login(client)
    original = FIXTURE.read_bytes()
    result = upload(client, headers, original, "概率、信息论与学习目标.md")
    assert result.status_code == 201, result.text
    document = result.json()
    assert document["filename"] == "概率、信息论与学习目标.md"
    assert document["size_bytes"] == len(original)
    assert document["progress"] == {"anchor": None, "offset": 0, "percent": 0, "updated_at": None}
    read = client.get(f"/api/documents/{document['id']}")
    assert read.status_code == 200
    assert read.json()["content"].encode("utf-8") == original
    assert read.json()["last_opened_at"].endswith("Z")
    download = client.get(f"/api/documents/{document['id']}/download")
    assert download.status_code == 200
    assert download.content == original
    assert "attachment" in download.headers["content-disposition"]
    assert download.headers["content-type"] == "text/markdown; charset=utf-8"
    assert (settings.data_dir / "documents" / f"{document['id']}.md").read_bytes() == original


def test_content_deduplication_and_distinct_versions(client):
    headers = login(client)
    first = upload(client, headers).json()
    duplicate = upload(client, headers)
    assert duplicate.status_code == 200
    assert duplicate.json()["id"] == first["id"]
    second = upload(client, headers, b"# A changed document")
    assert second.status_code == 201
    assert second.json()["id"] != first["id"]
    assert len(client.get("/api/documents").json()["documents"]) == 2


def test_uploads_preferences_progress_and_session_survive_restart(settings):
    client_id = str(uuid.uuid4())
    with TestClient(create_app(settings)) as first_client:
        headers = login(first_client)
        original = b"\xef\xbb\xbf# Persistent\r\n\r\n$E = mc^2$\r\n"
        document = upload(first_client, headers, original, "saved.markdown").json()
        document_id = document["id"]
        assert document["title"] == "Persistent"
        progress = first_client.put(f"/api/documents/{document_id}/progress", json={"anchor": "heading-2", "offset": 0.25, "percent": 48.5, "client_id": client_id, "sequence": 7}, headers=headers)
        assert progress.status_code == 200
        assert progress.json()["updated_at"].endswith("Z")
        assert first_client.put("/api/preferences", json={"font_size": 22, "theme": "dark"}, headers=headers).status_code == 200
        cookie = first_client.cookies.get(COOKIE_NAME)
    with TestClient(create_app(settings)) as second_client:
        second_client.cookies.set(COOKIE_NAME, cookie)
        assert second_client.get("/api/session").json()["csrf_token"] == headers["X-CSRF-Token"]
        assert second_client.get("/api/preferences").json() == {"font_size": 22, "theme": "dark"}
        saved = second_client.get(f"/api/documents/{document_id}").json()
        assert saved["content"].encode("utf-8") == original
        assert saved["progress"] == progress.json()
        assert second_client.get(f"/api/documents/{document_id}/download").content == original
        stale = second_client.put(f"/api/documents/{document_id}/progress", json={"anchor": "block-0", "offset": 0, "percent": 1, "client_id": client_id, "sequence": 6}, headers=headers)
        assert stale.json() == progress.json()


def test_csrf_required_for_every_mutation(client):
    headers = login(client)
    document_id = upload(client, headers).json()["id"]
    assert upload(client, {}).status_code == 403
    assert upload(client, {"X-CSRF-Token": "invalid"}).status_code == 403
    assert client.put("/api/preferences", json={"font_size": 18, "theme": "dark"}).status_code == 403
    assert client.put(f"/api/documents/{document_id}/progress", json={"anchor": None, "offset": 0, "percent": 0}).status_code == 403
    assert client.delete(f"/api/documents/{document_id}").status_code == 403
    assert client.post("/api/logout").status_code == 403


@pytest.mark.parametrize("request_headers", [
    {"Origin": "https://attacker.example"},
    {"Origin": "null"},
    {"Origin": "http://testserver.evil.example"},
    {"Origin": "http://testserver", "Sec-Fetch-Site": "cross-site"},
    {"Sec-Fetch-Site": "cross-site"},
])
def test_cross_site_login_rejected(client, request_headers):
    assert client.post("/api/login", json={"password": PASSWORD}, headers=request_headers).status_code == 403


def test_same_origin_and_explicit_proxy_origin_allowed(settings):
    settings = replace(settings, trusted_origins=("https://reader.example",))
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/api/login", json={"password": PASSWORD}, headers={"Origin": "https://reader.example"}).status_code == 200


def test_login_rate_limit_and_retry_after(settings):
    with TestClient(create_app(replace(settings, max_login_failures=2))) as client:
        for _ in range(2):
            assert client.post("/api/login", json={"password": "incorrect"}).status_code == 401
        limited = client.post("/api/login", json={"password": PASSWORD})
        assert limited.status_code == 429
        assert int(limited.headers["retry-after"]) > 0


def test_malformed_password_unicode_rejected(client):
    response = client.post("/api/login", content=b'{"password":"\\ud800"}', headers={"Content-Type": "application/json"})
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


@pytest.mark.parametrize("filename,content", [
    ("empty.md", b""),
    ("empty.md", b" \t\r\n"),
    ("bom.md", b"\xef\xbb\xbf\n"),
    ("spaces.md", "\u3000\u2002\n".encode("utf-8")),
    ("plain.txt", b"# Hello"),
    ("binary.md", b"\xff\xfe\x00"),
    ("binary.md", b"abc\x00def"),
    ("../outside.md", b"# Hello"),
    ("nested/name.md", b"# Hello"),
    ("nested\\name.md", b"# Hello"),
])
def test_invalid_uploads_have_no_persisted_side_effect(client, settings, filename, content):
    result = upload(client, login(client), content, filename)
    assert result.status_code == 400, result.text
    assert isinstance(result.json()["detail"], str)
    assert client.get("/api/documents").json() == {"documents": []}
    assert list((settings.data_dir / "documents").iterdir()) == []


def test_raw_control_character_filename_rejected(client):
    headers = login(client)
    body = b'--test\r\nContent-Disposition: form-data; name="file"; filename="bad\x01name.md"\r\nContent-Type: text/markdown\r\n\r\n# Hello\r\n--test--\r\n'
    result = client.post("/api/documents", content=body, headers={**headers, "Content-Type": "multipart/form-data; boundary=test"})
    assert result.status_code == 400


def test_upload_limit_exact_size_and_chunked_request_body(settings):
    with TestClient(create_app(replace(settings, max_upload_bytes=32))) as client:
        headers = login(client)
        assert upload(client, headers, b"a" * 32).status_code == 201
        assert upload(client, headers, b"a" * 33).status_code == 413
        # No Content-Length: enforce the request bound while receiving chunks.
        response = client.post("/api/documents", content=iter([b"x" * 40000, b"x" * 40000]), headers={**headers, "Content-Type": "multipart/form-data; boundary=test"})
        assert response.status_code == 413
        assert client.post("/api/login", content=b"x" * 65537, headers={"Content-Type": "application/json"}).status_code == 413


@pytest.mark.parametrize("content,expected", [
    ("```python\n# Not a heading\n```\n# Actual **title** ##\n", "Actual title"),
    ("~~~python\n# Not a heading\n~~~\n## Actual title\n", "Actual title"),
    ("    # An indented comment\n\n# Actual title\n", "Actual title"),
    ("Title with `code`\n=================\n", "Title with code"),
    ("No heading here", "fallback"),
])
def test_document_title_excludes_code_comments(client, content, expected):
    result = upload(client, login(client), content.encode("utf-8"), "fallback.md")
    assert result.status_code == 201
    assert result.json()["title"] == expected


@pytest.mark.parametrize("endpoint,payload", [
    ("/api/preferences", {"font_size": 15, "theme": "dark"}),
    ("/api/preferences", {"font_size": 25, "theme": "dark"}),
    ("/api/preferences", {"font_size": "18", "theme": "dark"}),
    ("/api/preferences", {"font_size": 18, "theme": "unknown"}),
    ("/api/documents/{id}/progress", {"anchor": "../../etc/passwd", "offset": 0, "percent": 0}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 1.1, "percent": 0}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": -1}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": 101}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": 0, "client_id": "valid-client-identifier"}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": 0, "sequence": 1}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": 0, "client_id": "valid-client-identifier", "sequence": -1}),
    ("/api/documents/{id}/progress", {"anchor": None, "offset": 0, "percent": 0, "client_id": "valid-client-identifier", "sequence": 9007199254740992}),
])
def test_progress_and_preferences_validation(client, endpoint, payload):
    headers = login(client)
    document_id = upload(client, headers).json()["id"]
    response = client.put(endpoint.format(id=document_id), json=payload, headers=headers)
    assert response.status_code == 422
    assert isinstance(response.json()["detail"], str)


def test_delete_removes_bytes_metadata_and_progress(client, settings):
    headers = login(client)
    document_id = upload(client, headers).json()["id"]
    assert client.put(f"/api/documents/{document_id}/progress", json={"anchor": "block-0", "offset": 0.5, "percent": 50, "client_id": str(uuid.uuid4()), "sequence": 1}, headers=headers).status_code == 200
    assert client.delete(f"/api/documents/{document_id}", headers=headers).status_code == 204
    assert client.get(f"/api/documents/{document_id}").status_code == 404
    assert client.get(f"/api/documents/{document_id}/download").status_code == 404
    assert client.delete(f"/api/documents/{document_id}", headers=headers).status_code == 404
    assert list((settings.data_dir / "documents").iterdir()) == []
    with sqlite3.connect(settings.data_dir / "reader.sqlite3") as conn:
        assert conn.execute("SELECT count(*) FROM progress").fetchone()[0] == 0
        assert conn.execute("SELECT count(*) FROM progress_writes").fetchone()[0] == 0


def test_out_of_order_progress_keeps_newer_same_page_write(client):
    headers = login(client)
    document_id = upload(client, headers).json()["id"]
    first_client_id, second_client_id = str(uuid.uuid4()), str(uuid.uuid4())
    path = f"/api/documents/{document_id}/progress"

    def write(percent, client_id, sequence):
        return client.put(path, json={"anchor": "block-0", "offset": 0, "percent": percent, "client_id": client_id, "sequence": sequence}, headers=headers)

    newest = write(70, first_client_id, 2)
    assert newest.status_code == 200
    assert write(30, first_client_id, 1).json() == newest.json()
    assert write(10, first_client_id, 2).json() == newest.json()
    assert client.get(f"/api/documents/{document_id}").json()["progress"] == newest.json()
    other_page = write(40, second_client_id, 1)
    assert other_page.json()["percent"] == 40
    assert write(30, first_client_id, 1).json() == other_page.json()
    assert write(90, first_client_id, 3).json()["percent"] == 90
    # The original three-field contract remains valid.
    legacy = client.put(path, json={"anchor": None, "offset": 0, "percent": 12}, headers=headers)
    assert legacy.status_code == 200 and legacy.json()["percent"] == 12


def test_logout_revokes_server_session_and_password_change_invalidates_sessions(settings):
    with TestClient(create_app(settings)) as client:
        headers = login(client)
        stale_cookie = client.cookies.get(COOKIE_NAME)
        assert client.post("/api/logout", headers=headers).status_code == 204
        client.cookies.set(COOKIE_NAME, stale_cookie)
        assert client.get("/api/documents").status_code == 401
        client.cookies.clear()
        login(client)
        cookie = client.cookies.get(COOKIE_NAME)
    with TestClient(create_app(replace(settings, password="changed password"))) as client:
        client.cookies.set(COOKIE_NAME, cookie)
        assert client.get("/api/session").json()["authenticated"] is False


def test_database_stores_session_hash_and_expired_cookie_is_cleared(client, settings):
    login(client)
    cookie = client.cookies.get(COOKIE_NAME)
    with sqlite3.connect(settings.data_dir / "reader.sqlite3") as conn:
        token_hash = conn.execute("SELECT token_hash FROM sessions").fetchone()[0]
        assert token_hash == hashlib.sha256(cookie.encode("ascii")).hexdigest()
        assert token_hash != cookie
        conn.execute("UPDATE sessions SET expires_at=0")
    response = client.get("/api/session")
    assert response.json()["authenticated"] is False
    assert "max-age=0" in response.headers["set-cookie"].lower()


def test_startup_requires_password_and_secure_cookie(settings):
    with pytest.raises(RuntimeError, match="READER_PASSWORD"):
        with TestClient(create_app(replace(settings, password=""))):
            pass
    with TestClient(create_app(replace(settings, cookie_secure=True)), base_url="https://testserver") as client:
        response = client.post("/api/login", json={"password": PASSWORD})
        cookie = response.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert client.get("/api/documents").status_code == 200


def test_static_files_and_api_404_security_headers(settings):
    settings.static_dir.mkdir()
    (settings.static_dir / "index.html").write_text("<h1>Reader</h1>", encoding="utf-8")
    (settings.static_dir / "assets").mkdir()
    (settings.static_dir / "assets" / "app.js").write_text("console.log('reader')", encoding="utf-8")
    outside = settings.static_dir.parent / "private.txt"
    outside.write_text("secret", encoding="utf-8")
    (settings.static_dir / "escaped.txt").symlink_to(outside)
    with TestClient(create_app(settings)) as client:
        home = client.get("/")
        assert home.status_code == 200
        assert home.text == "<h1>Reader</h1>"
        assert client.get("/books/a").text == home.text
        assert client.get("/api/unknown").status_code == 404
        assert client.get("/missing.js").status_code == 404
        assert client.get("/escaped.txt").status_code == 404
        assert client.get("/%2e%2e/private.txt").status_code == 404
        assert "immutable" in client.get("/assets/app.js").headers["cache-control"]
        assert client.get("/api/session").headers["cache-control"] == "no-store"
        assert home.headers["x-content-type-options"] == "nosniff"
        assert "script-src 'self';" in home.headers["content-security-policy"]
        assert "style-src 'self' 'unsafe-inline';" in home.headers["content-security-policy"]


def test_interrupted_deletion_restores_file_and_orphan_upload_is_cleaned(settings):
    with TestClient(create_app(settings)) as client:
        document_id = upload(client, login(client)).json()["id"]
    documents_dir = settings.data_dir / "documents"
    saved_path = documents_dir / f"{document_id}.md"
    saved_path.rename(saved_path.with_suffix(".trash"))
    orphan = documents_dir / f"{uuid.uuid4()}.md"
    orphan.write_bytes(b"uncommitted")
    partial = documents_dir / f"{uuid.uuid4()}.tmp"
    partial.write_bytes(b"partial upload")
    with TestClient(create_app(settings)) as client:
        login(client)
        assert client.get(f"/api/documents/{document_id}").status_code == 200
        assert saved_path.is_file()
        assert not orphan.exists() and not partial.exists()
