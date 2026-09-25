"""Loopback-only, zero-LLM browser portal for rarity watch management."""
from __future__ import annotations

import argparse
import html
import importlib.util
import json
from pathlib import Path
import re
import secrets
from typing import Any, Callable
from urllib.parse import parse_qs
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HOME = Path(__file__).resolve().parents[1]
STATE = HOME / "price-watches" / "mcfarlane-exotics.json"
LOCK = HOME / "price-watches" / ".exotic-execution-lock"
CSRF_SECRET = HOME / "cache" / "rarity-watch-portal-csrf.key"
MAX_REQUEST_BYTES = 20_000


def _manager_execute(request: dict[str, Any], **dependencies: Any) -> dict[str, Any]:
    module_path = Path(__file__).with_name("rarity_watch_manager.py")
    spec = importlib.util.spec_from_file_location("rarity_watch_portal_manager", module_path)
    manager = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(manager)
    return manager.execute(request, **dependencies)


def _persistent_csrf_token(path: Path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("x", encoding="utf-8") as handle:
            handle.write(secrets.token_urlsafe(32) + "\n")
        try:
            path.chmod(0o600)
        except OSError:
            pass
    except FileExistsError:
        pass
    token = path.read_text(encoding="utf-8").strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{40,64}", token):
        raise RuntimeError("portal CSRF secret is invalid")
    return token


def make_server(*, state_path: Path = STATE, lock_path: Path = LOCK,
                execute_fn: Callable[..., dict[str, Any]] = _manager_execute,
                port: int = 8767, csrf_path: Path = CSRF_SECRET) -> ThreadingHTTPServer:
    token = _persistent_csrf_token(csrf_path)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args: Any) -> None:
            pass

        def allowed_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def page(self, result: str = "Ready. List Saved reads disk only; no marketplace calls or notifications.", code: int = 200) -> None:
            content = '''<!doctype html><html><head><meta charset="utf-8"><title>RARITY WATCH MANAGER</title><style>
body{font:16px system-ui;max-width:900px;margin:0;padding:25px;background:#111827;color:#eee}input,select,button{font:inherit;padding:10px;border-radius:6px;border:1px solid #4b5563;background:#1f2937;color:#eee}label{display:grid;gap:5px}.grid{display:grid;grid-template-columns:2fr 1fr;gap:12px}.actions{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin:16px 0}button{cursor:pointer}pre{white-space:pre-wrap;padding:12px;background:#1f2937;border:1px solid #374151;border-radius:6px}small{color:#9ca3af}@media(max-width:700px){.grid,.actions{grid-template-columns:1fr}}
</style></head><body><h1>Exotic / Legendary Watch Manager</h1><p>Zero LLM tokens. Add completes only after the baseline is verified; it enables subsequent interval/8AM heartbeat coverage and posts a read-back-verified availability result to Discord. If Discord is temporarily unavailable, the watch remains enabled and the result is shown as pending for an exact retry. Disable, Remove, and List Saved are saved-state only: no marketplace calls and no notifications.</p>
<form method="post" action="/execute"><input type="hidden" name="csrf" value="{token}"><div class="grid">
<label>Polygon contract<input name="contract" placeholder="0x + 40 hexadecimal characters"></label>
<label>Rarity<select name="rarity"><option value="Exotic">Exotic</option><option value="Legendary">Legendary</option></select></label>
<label>Optional display name<input name="display"></label><label>Optional category<input name="category"></label></div>
<div class="actions"><button name="action" value="add">Add (Live)</button><button name="action" value="disable">Disable</button><button name="action" value="remove">Remove</button><button name="action" value="list" formnovalidate>List Saved</button></div></form>
<h2>Result</h2><pre id="result" aria-live="polite">{result}</pre></body></html>'''
            payload = content.replace("{token}", token).replace("{result}", html.escape(result)).encode()
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            expected_origin = "http://" + f"127.0.0.1:{self.server.server_port}"
            if not self.allowed_host() or self.path != "/execute" or self.headers.get("Origin") != expected_origin:
                self.send_error(403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= MAX_REQUEST_BYTES:
                    raise ValueError("Invalid request size")
                data = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
                if not secrets.compare_digest(data.get("csrf", [""])[0], token):
                    self.send_error(403)
                    return
                action = data.get("action", [""])[0]
                if action not in {"add", "disable", "remove", "list"}:
                    raise ValueError(f"unsupported action: {action}")
                request = {"action": action}
                if action != "list":
                    contract = data.get("contract", [""])[0].strip()
                    rarity = data.get("rarity", [""])[0]
                    if not re.fullmatch(r"0x[0-9a-fA-F]{40}", contract):
                        raise ValueError("Polygon contract must be 0x followed by exactly 40 hexadecimal characters")
                    if rarity not in {"Exotic", "Legendary"}:
                        raise ValueError("rarity must be exactly Exotic or Legendary")
                    request.update(contract=contract, rarity=rarity)
                    if action == "add":
                        for field in ("display", "category"):
                            value = data.get(field, [""])[0].strip()
                            if value:
                                request[field] = value
                result = execute_fn(request, state_path=Path(state_path), lock_path=Path(lock_path))
                if action == "list":
                    rows = [f"{watch.get('enabled') and 'ON ' or 'OFF'} {watch.get('rarity')} {watch.get('name')}  {watch.get('contract')}" for watch in result.get("watches", [])]
                    text = "\n".join([f"{result['count']} saved watch(es)", *rows,
                                      f"network calls: {result.get('network_calls', 0)}",
                                      f"notifications: {result.get('notifications', 0)}"])
                else:
                    text = json.dumps(result, indent=2)
                self.page(text)
            except (UnicodeDecodeError, ValueError, KeyError) as exc:
                self.page(str(exc), 400)
            except Exception as exc:
                self.page(str(exc)[:500] or "Operation failed", 503)

        def do_GET(self) -> None:
            if not self.allowed_host():
                self.send_error(403)
                return
            if self.path == "/health":
                payload = b'{"ok":true,"service":"rarity-watch-manager","llm_tokens":0}\n'
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(payload)
                return
            if self.path != "/":
                self.send_error(404)
                return
            self.page()

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def main() -> int:
    parser = argparse.ArgumentParser(description="Zero-LLM rarity watch browser portal")
    parser.add_argument("--serve", action="store_true")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--state", type=Path, default=STATE)
    parser.add_argument("--lock", type=Path, default=LOCK)
    args = parser.parse_args()
    if not args.serve:
        parser.error("--serve is required")
    server = make_server(state_path=args.state, lock_path=args.lock, port=args.port)
    print("http://" + f"127.0.0.1:{server.server_port}", flush=True)
    server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
