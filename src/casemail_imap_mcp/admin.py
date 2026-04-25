from __future__ import annotations

from pathlib import Path
import secrets
from typing import Callable

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse
from starlette.routing import Route

from .cache import PlainSyncStore
from .config import Settings
from .imap_client import ReadOnlyImapClient
from .security import FolderAccessController
from .sync_service import SyncService


SettingsReloader = Callable[[Settings], None]


def admin_routes(settings: Settings, reload_settings: SettingsReloader) -> list[Route]:
    async def admin_page(request: Request) -> HTMLResponse:
        return HTMLResponse(ADMIN_HTML)

    async def get_config(request: Request) -> JSONResponse:
        current = Settings()
        store = PlainSyncStore(current)
        return JSONResponse(_config_payload(current, store))

    async def post_config(request: Request) -> JSONResponse:
        payload = await request.json()
        env_path = Path(".env")
        existing = _read_env(env_path)
        values = {
            "APP_HOST": str(payload.get("app_host", existing.get("APP_HOST", "0.0.0.0"))),
            "APP_PORT": str(payload.get("app_port", existing.get("APP_PORT", "8000"))),
            "IMAP_HOST": str(payload.get("imap_host", "")).strip(),
            "IMAP_PORT": str(payload.get("imap_port", 993)).strip(),
            "IMAP_USERNAME": str(payload.get("imap_username", "")).strip(),
            "IMAP_USE_SSL": _bool_to_env(payload.get("imap_use_ssl", True)),
            "CASE_FOLDER_ALLOWLIST_REGEX": str(payload.get("case_folder_allowlist_regex", ".+")).strip() or ".+",
            "SENT_FOLDER_ALLOWLIST_REGEX": str(payload.get("sent_folder_allowlist_regex", r"^(Sent|Sent Items)$")).strip(),
            "DEFAULT_SENT_FOLDERS": str(payload.get("default_sent_folders", "Sent,Sent Items")).strip(),
            "ALLOW_GLOBAL_SEARCH": "false",
            "MAX_ATTACHMENT_BYTES": str(payload.get("max_attachment_bytes", existing.get("MAX_ATTACHMENT_BYTES", "10485760"))),
            "MAX_TOTAL_SYNC_BYTES_PER_RUN": str(
                payload.get("max_total_sync_bytes_per_run", existing.get("MAX_TOTAL_SYNC_BYTES_PER_RUN", "1073741824"))
            ),
            "CACHE_ENABLED": "true",
            "CACHE_DB_PATH": str(payload.get("cache_db_path", existing.get("CACHE_DB_PATH", ".cache/casemail_cache.sqlite3"))),
        }
        password = str(payload.get("imap_password", ""))
        if password:
            values["IMAP_PASSWORD"] = password
        elif "IMAP_PASSWORD" in existing:
            values["IMAP_PASSWORD"] = existing["IMAP_PASSWORD"]
        else:
            values["IMAP_PASSWORD"] = ""
        values["MESSAGE_REF_SECRET"] = existing.get("MESSAGE_REF_SECRET") or secrets.token_urlsafe(32)

        _write_env(env_path, existing, values)
        new_settings = Settings()
        reload_settings(new_settings)
        store = PlainSyncStore(new_settings)
        return JSONResponse(_config_payload(new_settings, store))

    async def test_connection(request: Request) -> JSONResponse:
        current = Settings()
        if not current.imap_host or not current.imap_username or not current.imap_password:
            return JSONResponse({"ok": False, "error": "IMAP credentials are incomplete."}, status_code=400)
        try:
            with ReadOnlyImapClient(current) as client:
                client.noop()
                folders = client.list_folders(include_counts=False)
            return JSONResponse({"ok": True, "folder_count": len(folders)})
        except Exception as exc:
            return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)

    async def list_remote_folders(request: Request) -> JSONResponse:
        current = Settings()
        store = PlainSyncStore(current)
        selected = set(store.list_selected_folders())
        access = FolderAccessController(current)
        try:
            with ReadOnlyImapClient(current) as client:
                folders = client.list_folders(include_counts=True)
            response_folders = []
            for folder in folders:
                store.upsert_folder_metadata(
                    folder.name,
                    delimiter=folder.delimiter,
                    flags=folder.flags,
                    message_count=folder.message_count,
                    uidvalidity=folder.uidvalidity,
                )
                response_folders.append(
                    {
                        "name": folder.name,
                        "delimiter": folder.delimiter,
                        "flags": folder.flags,
                        "message_count": folder.message_count,
                        "uidvalidity": folder.uidvalidity,
                        "selected": folder.name in selected,
                        "is_sent_candidate": access.is_sent_folder_allowed(folder.name),
                    }
                )
            return JSONResponse({"folders": response_folders})
        except Exception as exc:
            return JSONResponse({"folders": [], "error": str(exc)}, status_code=400)

    async def save_selected_folders(request: Request) -> JSONResponse:
        payload = await request.json()
        folders = payload.get("folders", [])
        if not isinstance(folders, list):
            return JSONResponse({"error": "folders must be a list"}, status_code=400)
        current = Settings()
        store = PlainSyncStore(current)
        store.set_selected_folders([str(folder) for folder in folders])
        return JSONResponse({"selected_folders": store.list_selected_folders()})

    async def sync_selected(request: Request) -> JSONResponse:
        current = Settings()
        if not current.imap_host or not current.imap_username or not current.imap_password:
            return JSONResponse({"error": "IMAP credentials are incomplete."}, status_code=400)
        store = PlainSyncStore(current)
        result = SyncService(current, store).sync_selected_folders()
        return JSONResponse(result)

    async def sync_status(request: Request) -> JSONResponse:
        current = Settings()
        return JSONResponse(PlainSyncStore(current).get_sync_status())

    return [
        Route("/admin", admin_page, methods=["GET"]),
        Route("/admin/api/config", get_config, methods=["GET"]),
        Route("/admin/api/config", post_config, methods=["POST"]),
        Route("/admin/api/test-connection", test_connection, methods=["POST"]),
        Route("/admin/api/folders", list_remote_folders, methods=["GET"]),
        Route("/admin/api/selected-folders", save_selected_folders, methods=["POST"]),
        Route("/admin/api/sync", sync_selected, methods=["POST"]),
        Route("/admin/api/sync-status", sync_status, methods=["GET"]),
    ]


def _config_payload(settings: Settings, store: PlainSyncStore) -> dict[str, object]:
    return {
        "app_host": settings.app_host,
        "app_port": settings.app_port,
        "imap_host": settings.imap_host,
        "imap_port": settings.imap_port,
        "imap_username": settings.imap_username,
        "imap_password_configured": bool(settings.imap_password),
        "imap_use_ssl": settings.imap_use_ssl,
        "case_folder_allowlist_regex": settings.case_folder_allowlist_regex,
        "sent_folder_allowlist_regex": settings.sent_folder_allowlist_regex,
        "default_sent_folders": settings.default_sent_folders,
        "max_attachment_bytes": settings.max_attachment_bytes,
        "max_total_sync_bytes_per_run": settings.max_total_sync_bytes_per_run,
        "cache_db_path": str(settings.cache_db_path),
        "selected_folders": store.list_selected_folders(),
        "sync_status": store.get_sync_status(),
    }


def _read_env(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line or line.lstrip().startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


def _write_env(path: Path, existing: dict[str, str], updates: dict[str, str]) -> None:
    ordered_keys = [
        "APP_HOST",
        "APP_PORT",
        "IMAP_HOST",
        "IMAP_PORT",
        "IMAP_USERNAME",
        "IMAP_PASSWORD",
        "IMAP_USE_SSL",
        "CASE_FOLDER_ALLOWLIST_REGEX",
        "SENT_FOLDER_ALLOWLIST_REGEX",
        "DEFAULT_SENT_FOLDERS",
        "ALLOW_GLOBAL_SEARCH",
        "MAX_ATTACHMENT_BYTES",
        "MAX_TOTAL_SYNC_BYTES_PER_RUN",
        "CACHE_ENABLED",
        "CACHE_DB_PATH",
        "MESSAGE_REF_SECRET",
    ]
    merged = {**existing, **updates}
    lines = ["# Generated by CaseMail IMAP local admin UI"]
    for key in ordered_keys:
        if key in merged:
            lines.append(f"{key}={merged[key]}")
    for key in sorted(key for key in merged if key not in ordered_keys):
        lines.append(f"{key}={merged[key]}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bool_to_env(value: object) -> str:
    return "true" if bool(value) else "false"


ADMIN_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>CaseMail IMAP Admin</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #1f2933;
      --muted: #5f6f7f;
      --line: #d7dee6;
      --panel: #f7f9fb;
      --accent: #0f766e;
      --accent-strong: #0b5f59;
      --danger: #a43e2b;
      --surface: #ffffff;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: "Aptos", "Segoe UI", sans-serif;
      color: var(--ink);
      background: linear-gradient(180deg, #eef3f6 0%, #ffffff 28%);
    }
    header, main { width: min(1180px, calc(100vw - 32px)); margin: 0 auto; }
    header { padding: 28px 0 14px; display: flex; align-items: end; justify-content: space-between; gap: 20px; }
    h1 { margin: 0; font-size: 28px; font-weight: 700; letter-spacing: 0; }
    h2 { margin: 0 0 14px; font-size: 17px; letter-spacing: 0; }
    .status { color: var(--muted); font-size: 14px; }
    main { display: grid; grid-template-columns: 380px 1fr; gap: 18px; padding: 12px 0 36px; }
    section { background: var(--surface); border: 1px solid var(--line); border-radius: 8px; padding: 18px; box-shadow: 0 10px 30px rgba(31, 41, 51, 0.05); }
    label { display: block; font-size: 13px; font-weight: 650; margin: 12px 0 6px; }
    input, select {
      width: 100%;
      min-height: 38px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 8px 10px;
      font: inherit;
      background: #fff;
    }
    .row { display: grid; grid-template-columns: 1fr 96px; gap: 10px; }
    .checkline { display: flex; align-items: center; gap: 8px; margin: 12px 0; color: var(--muted); }
    .checkline input { width: auto; min-height: auto; }
    button {
      border: 0;
      border-radius: 6px;
      min-height: 38px;
      padding: 8px 12px;
      font: inherit;
      font-weight: 700;
      cursor: pointer;
      background: var(--accent);
      color: #fff;
    }
    button.secondary { background: #334e68; }
    button.ghost { background: #eef3f6; color: var(--ink); }
    button:disabled { opacity: 0.55; cursor: wait; }
    .actions { display: flex; flex-wrap: wrap; gap: 10px; margin-top: 16px; }
    .folders { max-height: 520px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; }
    .folder {
      display: grid;
      grid-template-columns: 28px 1fr auto;
      gap: 10px;
      align-items: center;
      padding: 10px 12px;
      border-bottom: 1px solid var(--line);
    }
    .folder:last-child { border-bottom: 0; }
    .folder input { width: auto; min-height: auto; }
    .folder-name { font-weight: 650; word-break: break-word; }
    .folder-meta { color: var(--muted); font-size: 12px; margin-top: 2px; }
    .badge { background: var(--panel); border: 1px solid var(--line); border-radius: 999px; padding: 3px 8px; font-size: 12px; color: var(--muted); }
    pre {
      white-space: pre-wrap;
      word-break: break-word;
      background: #17212b;
      color: #e7eef5;
      border-radius: 8px;
      padding: 12px;
      min-height: 160px;
      max-height: 300px;
      overflow: auto;
      font-size: 12px;
    }
    .grid { display: grid; grid-template-columns: 1fr; gap: 18px; }
    @media (max-width: 880px) {
      main { grid-template-columns: 1fr; }
      header { display: block; }
    }
  </style>
</head>
<body>
  <header>
    <div>
      <h1>CaseMail IMAP</h1>
      <div class="status" id="topStatus">Local admin</div>
    </div>
    <button class="ghost" id="refreshStatus">Refresh status</button>
  </header>
  <main>
    <section>
      <h2>IMAP connection</h2>
      <label for="imapHost">Host</label>
      <input id="imapHost" autocomplete="off">
      <div class="row">
        <div>
          <label for="imapUser">Username</label>
          <input id="imapUser" autocomplete="username">
        </div>
        <div>
          <label for="imapPort">Port</label>
          <input id="imapPort" type="number" min="1">
        </div>
      </div>
      <label for="imapPassword">Password</label>
      <input id="imapPassword" type="password" autocomplete="current-password" placeholder="Leave blank to keep existing password">
      <div class="checkline">
        <input id="imapSsl" type="checkbox">
        <span>Use SSL/TLS</span>
      </div>
      <label for="caseRegex">Case folder allowlist regex</label>
      <input id="caseRegex">
      <label for="sentRegex">Sent folder regex</label>
      <input id="sentRegex">
      <label for="defaultSent">Default sent folders</label>
      <input id="defaultSent">
      <div class="actions">
        <button id="saveConfig">Save config</button>
        <button class="secondary" id="testConnection">Test connection</button>
      </div>
    </section>
    <div class="grid">
      <section>
        <h2>Folders to sync</h2>
        <div class="actions">
          <button class="secondary" id="loadFolders">Load folders</button>
          <button class="ghost" id="saveFolders">Save selection</button>
          <button id="syncNow">Sync messages and attachments</button>
        </div>
        <label for="folderFilter">Filter</label>
        <input id="folderFilter" placeholder="Type to filter folders">
        <div class="folders" id="folders"></div>
      </section>
      <section>
        <h2>Sync status</h2>
        <pre id="syncStatus">{}</pre>
      </section>
    </div>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let folders = [];

    async function api(path, options = {}) {
      const response = await fetch(path, {
        headers: { "content-type": "application/json" },
        ...options,
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || JSON.stringify(data));
      return data;
    }

    function setBusy(button, busy) {
      button.disabled = busy;
    }

    function renderFolders() {
      const filter = $("folderFilter").value.toLowerCase();
      const root = $("folders");
      root.innerHTML = "";
      for (const folder of folders.filter((item) => item.name.toLowerCase().includes(filter))) {
        const row = document.createElement("label");
        row.className = "folder";
        row.innerHTML = `
          <input type="checkbox" data-folder="${folder.name.replaceAll('"', '&quot;')}" ${folder.selected ? "checked" : ""}>
          <div>
            <div class="folder-name"></div>
            <div class="folder-meta"></div>
          </div>
          <span class="badge">${folder.is_sent_candidate ? "sent" : "mail"}</span>
        `;
        row.querySelector(".folder-name").textContent = folder.name;
        row.querySelector(".folder-meta").textContent = `${folder.message_count ?? "?"} messages`;
        root.appendChild(row);
      }
    }

    async function loadConfig() {
      const config = await api("/admin/api/config");
      $("imapHost").value = config.imap_host || "";
      $("imapPort").value = config.imap_port || 993;
      $("imapUser").value = config.imap_username || "";
      $("imapSsl").checked = !!config.imap_use_ssl;
      $("caseRegex").value = config.case_folder_allowlist_regex || ".+";
      $("sentRegex").value = config.sent_folder_allowlist_regex || "^(Sent|Sent Items)$";
      $("defaultSent").value = config.default_sent_folders || "Sent,Sent Items";
      $("topStatus").textContent = config.imap_password_configured ? "Password configured" : "Password not configured";
      $("syncStatus").textContent = JSON.stringify(config.sync_status, null, 2);
    }

    async function loadFolders() {
      folders = (await api("/admin/api/folders")).folders;
      renderFolders();
    }

    $("folderFilter").addEventListener("input", renderFolders);
    $("refreshStatus").addEventListener("click", async () => {
      $("syncStatus").textContent = JSON.stringify(await api("/admin/api/sync-status"), null, 2);
    });
    $("saveConfig").addEventListener("click", async (event) => {
      setBusy(event.target, true);
      try {
        const payload = {
          imap_host: $("imapHost").value,
          imap_port: $("imapPort").value,
          imap_username: $("imapUser").value,
          imap_password: $("imapPassword").value,
          imap_use_ssl: $("imapSsl").checked,
          case_folder_allowlist_regex: $("caseRegex").value,
          sent_folder_allowlist_regex: $("sentRegex").value,
          default_sent_folders: $("defaultSent").value,
        };
        const config = await api("/admin/api/config", { method: "POST", body: JSON.stringify(payload) });
        $("imapPassword").value = "";
        $("syncStatus").textContent = JSON.stringify(config.sync_status, null, 2);
        $("topStatus").textContent = "Config saved";
      } catch (error) {
        $("topStatus").textContent = error.message;
      } finally {
        setBusy(event.target, false);
      }
    });
    $("testConnection").addEventListener("click", async (event) => {
      setBusy(event.target, true);
      try {
        const result = await api("/admin/api/test-connection", { method: "POST", body: "{}" });
        $("topStatus").textContent = `Connected: ${result.folder_count} folders`;
      } catch (error) {
        $("topStatus").textContent = error.message;
      } finally {
        setBusy(event.target, false);
      }
    });
    $("loadFolders").addEventListener("click", async (event) => {
      setBusy(event.target, true);
      try { await loadFolders(); } catch (error) { $("topStatus").textContent = error.message; }
      finally { setBusy(event.target, false); }
    });
    $("saveFolders").addEventListener("click", async () => {
      const selected = [...document.querySelectorAll("[data-folder]:checked")].map((item) => item.dataset.folder);
      await api("/admin/api/selected-folders", { method: "POST", body: JSON.stringify({ folders: selected }) });
      $("topStatus").textContent = `${selected.length} folders selected`;
    });
    $("syncNow").addEventListener("click", async (event) => {
      setBusy(event.target, true);
      try {
        const selected = [...document.querySelectorAll("[data-folder]:checked")].map((item) => item.dataset.folder);
        if (selected.length) {
          await api("/admin/api/selected-folders", { method: "POST", body: JSON.stringify({ folders: selected }) });
        }
        const result = await api("/admin/api/sync", { method: "POST", body: "{}" });
        $("syncStatus").textContent = JSON.stringify(result, null, 2);
        $("topStatus").textContent = result.state;
      } catch (error) {
        $("topStatus").textContent = error.message;
      } finally {
        setBusy(event.target, false);
      }
    });
    loadConfig().catch((error) => $("topStatus").textContent = error.message);
  </script>
</body>
</html>"""

