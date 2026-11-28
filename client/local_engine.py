"""
NTTM_PLUS - Local Browsing Engine (Phase 1)

Runs Chromium locally on the user's own machine via Playwright, providing
the exact same FetchResult contract as the GitHub Actions transport:

- navigate / form / replay / ajax_fallback modes (parity with remote_runner.mjs)
- session transfer (cookies / localStorage / sessionStorage) via SessionManager
- SPA action replay, custom delay, auto scroll
- file downloads saved into the configured downloads dir
- CDP attach mode: drive the user's already-running browser
  (set config.local_cdp_url or env NTTM_CDP_URL, e.g.
   chrome --remote-debugging-port=9222  =>  NTTM_CDP_URL=http://127.0.0.1:9222)

Offline rendering: sub-resources (css/js/img/font/media) seen during load are
captured into the request cache dir and the saved index.html is rewritten to
reference them with relative paths, so pages keep their look inside the
local UI shell.
"""

import base64
import hashlib
import json
import logging
import os
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .config import BrowserConfig
from .engine import FetchEngine, FetchResult
from .session_manager import SessionManager

logger = logging.getLogger("LocalEngine")

GOTO_TIMEOUT_MS = 45_000
NETWORK_IDLE_TIMEOUT_MS = 8_000
ACTION_TIMEOUT_MS = 8_000

MAX_ASSETS = 300
MAX_ASSET_BYTES = 15 * 1024 * 1024
MAX_TOTAL_ASSET_BYTES = 80 * 1024 * 1024

CT_EXT_MAP = {
    "text/css": ".css",
    "text/javascript": ".js",
    "application/javascript": ".js",
    "application/x-javascript": ".js",
    "application/json": ".json",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/svg+xml": ".svg",
    "image/webp": ".webp",
    "image/x-icon": ".ico",
    "image/vnd.microsoft.icon": ".ico",
    "font/woff": ".woff",
    "font/woff2": ".woff2",
    "font/ttf": ".ttf",
    "font/otf": ".otf",
    "application/font-woff": ".woff",
    "application/font-woff2": ".woff2",
    "video/mp4": ".mp4",
    "video/webm": ".webm",
    "audio/mpeg": ".mp3",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
}

SHORTS_NEXT_SELECTORS = [
    "#navigation-button-down",
    "button[aria-label='Next video']",
    "button[aria-label*='Next']",
]


class LocalEngine(FetchEngine):
    """Browsing engine that drives a local Chromium via Playwright."""

    name = "local"

    def __init__(self, config: BrowserConfig, session_manager: SessionManager):
        self.config = config
        self.session_manager = session_manager
        # Same cache root as GitHubTransport: the UI shell serves everything
        # under /cache/ from transport.cache_base, so parity is preserved.
        self.cache_base = Path.home() / ".gh_offline_browser" / "cache"
        self.cache_base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # URL helpers (same semantics as GitHubTransport.normalize_target_url)
    # ------------------------------------------------------------------
    def normalize_target_url(self, target_url: str) -> str:
        raw = target_url.strip()
        if not raw:
            return "https://www.google.com"
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        if " " not in raw and "." in raw and not raw.startswith("/"):
            return f"https://{raw}"
        query_encoded = urllib.parse.quote_plus(raw)
        search_template = self.config.default_search_url or "https://www.google.com/search?q={query}"
        if "{query}" in search_template:
            return search_template.format(query=query_encoded)
        return f"{search_template}{query_encoded}"

    # ------------------------------------------------------------------
    # Main entrypoint
    # ------------------------------------------------------------------
    def fetch(
        self,
        target_url: str,
        mode: str = "navigate",
        method: str = "GET",
        content_type: str = "",
        body_bytes: bytes = b"",
        actions: Optional[List[Dict[str, Any]]] = None,
        intercepted_ajax: Optional[Dict[str, Any]] = None,
        custom_headers: Optional[Dict[str, str]] = None,
        custom_delay_seconds: int = 0,
        custom_scroll_steps: int = 0,
        status_callback: Optional[Callable[[str], None]] = None,
    ) -> FetchResult:
        def update_status(msg: str):
            logger.info(msg)
            if status_callback:
                try:
                    status_callback(msg)
                except Exception as e:
                    logger.debug(f"Status callback error: {e}")

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return FetchResult(
                success=False,
                error_message=(
                    "Playwright نصب نشده است. اجرا کنید:\n"
                    "pip install playwright\n"
                    "python -m playwright install chromium"
                ),
            )

        final_target_url = self.normalize_target_url(target_url) if mode == "navigate" else target_url
        request_id = f"local_{int(time.time())}_{uuid.uuid4().hex[:6]}"
        cache_dir = self.cache_base / "local" / request_id
        cache_dir.mkdir(parents=True, exist_ok=True)
        asset_dir = cache_dir / "assets"
        asset_dir.mkdir(parents=True, exist_ok=True)

        update_status(f"موتور محلی فعال است — در حال آماده‌سازی Chromium برای {final_target_url}")

        storage_state, session_storage = self.session_manager.get_envelope_state(target_url=final_target_url)

        url_map: Dict[str, str] = {}
        total_asset_bytes = [0]
        downloaded_file_path: List[Optional[Path]] = [None]
        downloaded_file_name: List[Optional[str]] = [None]

        try:
            with sync_playwright() as p:
                browser, context = self._open_browser_context(
                    p,
                    storage_state=storage_state,
                    session_storage=session_storage,
                    update_status=update_status,
                )
                try:
                    page = context.new_page()

                    def on_response(resp):
                        self._capture_asset(resp, asset_dir, url_map, total_asset_bytes)

                    page.on("response", on_response)

                    def on_download(download):
                        self._save_download(download, downloaded_file_path, downloaded_file_name)

                    page.on("download", on_download)

                    # 1. Navigate / form / replay / ajax fallback
                    main_response = self._perform_navigation(
                        page,
                        context,
                        mode=mode,
                        url=final_target_url,
                        method=method,
                        content_type=content_type,
                        body_bytes=body_bytes,
                        actions=actions or [],
                        intercepted_ajax=intercepted_ajax,
                        custom_headers=custom_headers,
                        update_status=update_status,
                    )

                    # 2. File download detection (content-disposition or download event)
                    file_result = self._maybe_file_result(
                        page, main_response, downloaded_file_path, downloaded_file_name, update_status
                    )
                    if file_result is not None:
                        return file_result

                    # 3. Custom delay & auto scroll (parity with runner options)
                    if custom_delay_seconds and custom_delay_seconds > 0:
                        update_status(f"تأخیر {custom_delay_seconds} ثانیه‌ای روی صفحه...")
                        time.sleep(min(custom_delay_seconds, 120))
                    if custom_scroll_steps and custom_scroll_steps > 0:
                        update_status(f"اسکرول خودکار: {custom_scroll_steps} مرحله...")
                        self._auto_scroll(page, custom_scroll_steps)

                    # 4. Snapshot DOM and rewrite captured asset URLs
                    update_status("در حال ذخیره‌سازی snapshot صفحه...")
                    html = page.content()
                    html = self._rewrite_html(html, url_map)
                    index_path = cache_dir / "index.html"
                    index_path.write_text(html, encoding="utf-8", errors="ignore")
                    sha256 = hashlib.sha256(index_path.read_bytes()).hexdigest()

                    # 5. Persist session back (cookies + localStorage + sessionStorage)
                    try:
                        state_path = cache_dir / "storage_state.json"
                        if not getattr(self, "_is_cdp_attach", False):
                            context.storage_state(path=str(state_path))
                            self.session_manager.update_from_remote(state_path)
                        origin = ""
                        try:
                            origin = page.evaluate("location.origin")
                        except Exception:
                            origin = ""
                        if origin and origin.startswith("http"):
                            ss = page.evaluate(
                                "() => { const o = {}; for (let i = 0; i < sessionStorage.length; i++)"
                                " { const k = sessionStorage.key(i); o[k] = sessionStorage.getItem(k); } return o; }"
                            )
                            if isinstance(ss, dict) and ss:
                                self.session_manager.merge_client_delta(
                                    origin=origin, session_storage_dict={str(k): str(v) for k, v in ss.items()}
                                )
                    except Exception as e:
                        logger.warning(f"Session persist warning: {e}")

                    # 6. meta.json (debug artifact, mirrors runner output shape)
                    meta = {
                        "engine": "local",
                        "request_id": request_id,
                        "requested_url": final_target_url,
                        "final_url": page.url,
                        "title": (page.title() or "").strip(),
                        "sha256": sha256,
                        "size": index_path.stat().st_size,
                        "assets_captured": len(url_map),
                        "response_type": "page",
                        "timestamp": int(time.time()),
                    }
                    (cache_dir / "meta.json").write_text(
                        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
                    )

                    update_status(
                        f"صفحه با موتور محلی آماده شد ({meta['title'] or meta['final_url']}) "
                        f"— {len(url_map)} منبع ذخیره شد."
                    )
                    return FetchResult(
                        success=True,
                        response_type="page",
                        index_html_path=str(index_path),
                        meta=meta,
                    )
                finally:
                    try:
                        context.close()
                    except Exception:
                        pass
                    try:
                        browser.close()
                    except Exception:
                        pass
        except Exception as e:
            logger.exception("LocalEngine fetch failed")
            return FetchResult(success=False, error_message=f"خطای موتور محلی: {e}")

    fetch_page = fetch  # UI calls transport.fetch_page(...)

    # ------------------------------------------------------------------
    # Browser / context setup
    # ------------------------------------------------------------------
    def _open_browser_context(self, p, storage_state, session_storage, update_status):
        cdp_url = (getattr(self.config, "local_cdp_url", "") or "").strip() or os.environ.get("NTTM_CDP_URL", "").strip()

        if cdp_url:
            update_status(f"اتصال به مرورگر بازِ کاربر از طریق CDP ({cdp_url})...")
            browser = p.chromium.connect_over_cdp(cdp_url)
            self._is_cdp_attach = True
            if browser.contexts:
                context = browser.contexts[0]
            else:
                context = browser.new_context(viewport={"width": 1366, "height": 768})
            # In attach mode the user's own profile provides cookies/storage;
            # skip injecting SessionManager state to avoid clobbering it.
            return browser, context

        self._is_cdp_attach = False
        headless = bool(getattr(self.config, "local_headless", True))
        update_status("اجرای Chromium محلی" + (" (headless)..." if headless else " (پنجره مرورگر)..."))
        browser = p.chromium.launch(
            headless=headless,
            args=["--disable-blink-features=AutomationControlled", "--no-first-run", "--no-default-browser-check"],
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="en-US",
            accept_downloads=True,
            storage_state=storage_state if (storage_state and (storage_state.get("cookies") or storage_state.get("origins"))) else None,
        )
        # sessionStorage replay (Playwright storageState has no sessionStorage)
        for origin, items in (session_storage or {}).items():
            if not items:
                continue
            try:
                sets = "".join(
                    f"try{{sessionStorage.setItem({json.dumps(str(k))},{json.dumps(str(v))});}}catch(e){{}}"
                    for k, v in items.items()
                )
                context.add_init_script(f"if (location.origin === {json.dumps(str(origin))}) {{ {sets} }}")
            except Exception as e:
                logger.debug(f"Init script skipped for {origin}: {e}")
        return browser, context

    # ------------------------------------------------------------------
    # Navigation modes
    # ------------------------------------------------------------------
    def _perform_navigation(self, page, context, mode, url, method, content_type, body_bytes, actions, intercepted_ajax, custom_headers, update_status):
        main_response = None
        if mode == "form":
            self._submit_form(page, context, url, method, content_type, body_bytes, update_status)
        elif mode == "replay":
            update_status(f"ناوبری به {url} و اجرای {len(actions)} اکشن...")
            main_response = page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            self._best_effort_network_idle(page)
            self._run_actions(page, actions)
        elif mode == "ajax_fallback" and intercepted_ajax and intercepted_ajax.get("captured"):
            update_status("اجرای AJAX رهگیری‌شده...")
            self._execute_intercepted_ajax(page, url, intercepted_ajax)
        else:
            update_status(f"ناوبری به {url}...")
            main_response = page.goto(url, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            self._best_effort_network_idle(page)
        return main_response

    def _best_effort_network_idle(self, page):
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:
            pass  # SPAs with long-polling never reach networkidle

    def _submit_form(self, page, context, url, method, content_type, body_bytes, update_status):
        method = (method or "GET").upper()
        ctype = (content_type or "").lower()
        if method == "GET":
            sep = "&" if urllib.parse.urlparse(url).query else "?"
            page.goto(url + sep + body_bytes.decode("utf-8", "ignore"), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            self._best_effort_network_idle(page)
            return
        if "x-www-form-urlencoded" in ctype or not ctype:
            page.goto("about:blank", timeout=GOTO_TIMEOUT_MS)
            fields = urllib.parse.parse_qsl(body_bytes.decode("utf-8", "ignore"), keep_blank_values=True)
            page.evaluate(
                "(fields) => {"
                " const f = document.createElement('form');"
                f" f.method = {json.dumps(method)}; f.action = {json.dumps(url)};"
                " for (const [k, v] of fields) { const i = document.createElement('input');"
                " i.type = 'hidden'; i.name = k; i.value = v; f.appendChild(i); }"
                " document.body.appendChild(f); f.submit(); }",
                fields,
            )
        else:
            # JSON / binary bodies: send through the APIRequestContext and render.
            headers = {"Content-Type": content_type} if content_type else None
            resp = context.request.fetch(url, method=method, data=body_bytes, headers=headers, timeout=30_000)
            body = resp.text() or ""
            ctype_resp = (resp.headers or {}).get("content-type", "")
            if "html" in ctype_resp:
                page.set_content(body, wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)
            else:
                raise RuntimeError(f"پاسخ فرم HTML نبود ({ctype_resp or 'unknown'})")
        try:
            page.wait_for_load_state("domcontentloaded", timeout=GOTO_TIMEOUT_MS)
        except Exception:
            pass
        self._best_effort_network_idle(page)

    def _execute_intercepted_ajax(self, page, url, ajax):
        ajax_url = ajax.get("url") or url
        ajax_method = (ajax.get("method") or "GET").upper()
        ajax_headers = {str(k): str(v) for k, v in (ajax.get("headers") or {}).items()}
        ajax_body_b64 = ajax.get("body_base64") or ""
        ajax_body = base64.b64decode(ajax_body_b64) if ajax_body_b64 else None
        page.goto("about:blank", timeout=GOTO_TIMEOUT_MS)
        payload = json.dumps(
            {
                "url": ajax_url,
                "method": ajax_method,
                "headers": ajax_headers,
                "body": base64.b64encode(ajax_body or b"").decode("ascii"),
            }
        )
        html = page.evaluate(
            """async (cfg) => {
                const opts = { method: cfg.method, headers: cfg.headers };
                if (cfg.method !== 'GET' && cfg.body) {
                    const bin = atob(cfg.body); const arr = new Uint8Array(bin.length);
                    for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
                    opts.body = arr;
                }
                const r = await fetch(cfg.url, opts);
                return await r.text();
            }""",
            payload,
        )
        if html:
            page.set_content(str(html), wait_until="domcontentloaded", timeout=GOTO_TIMEOUT_MS)

    # ------------------------------------------------------------------
    # SPA action runner (subset parity of remote_runner.mjs)
    # ------------------------------------------------------------------
    def _run_actions(self, page, actions):
        for i, act in enumerate(actions):
            if not isinstance(act, dict):
                continue
            t = str(act.get("type") or "").lower()
            selector = str(act.get("selector") or act.get("target") or "")
            value = act.get("value") or act.get("text") or ""
            wait_ms = int(act.get("wait_after_ms") or act.get("wait_ms") or 500)
            try:
                if t in ("click", "tap"):
                    page.click(selector, timeout=ACTION_TIMEOUT_MS)
                elif t in ("fill", "input"):
                    page.fill(selector, str(value), timeout=ACTION_TIMEOUT_MS)
                elif t == "type":
                    page.type(selector, str(value), timeout=ACTION_TIMEOUT_MS)
                elif t == "press":
                    page.keyboard.press(str(value or "Enter"))
                elif t == "select":
                    page.select_option(selector, str(value), timeout=ACTION_TIMEOUT_MS)
                elif t == "wait":
                    time.sleep(min(max(wait_ms, 0), 30_000) / 1000)
                elif t == "scroll":
                    amount = int(act.get("amount") or value or 800)
                    page.mouse.wheel(0, amount)
                elif t == "evaluate":
                    page.evaluate(str(value))
                elif t == "shorts_next":
                    clicked = False
                    for sel in SHORTS_NEXT_SELECTORS:
                        try:
                            if page.locator(sel).count() > 0:
                                page.locator(sel).first.click(timeout=2_000)
                                clicked = True
                                break
                        except Exception:
                            continue
                    if not clicked:
                        page.keyboard.press("ArrowDown")
                else:
                    logger.warning(f"Unknown action type skipped: {t}")
            except Exception as e:
                logger.warning(f"Action {i + 1}/{len(actions)} '{t}' failed: {e}")
            if t not in ("wait",):
                try:
                    time.sleep(min(max(wait_ms, 0), 30_000) / 1000)
                except Exception:
                    pass

    def _auto_scroll(self, page, steps):
        for _ in range(max(1, min(steps, 60))):
            page.mouse.wheel(0, 900)
            try:
                page.wait_for_load_state("networkidle", timeout=1_500)
            except Exception:
                time.sleep(0.25)

    # ------------------------------------------------------------------
    # Asset capture & HTML rewriting
    # ------------------------------------------------------------------
    def _capture_asset(self, resp, asset_dir, url_map, total_bytes_ref):
        try:
            if len(url_map) >= MAX_ASSETS:
                return
            url = resp.url
            if not url.startswith(("http://", "https://")) or url in url_map:
                return
            rtype = ""
            try:
                rtype = resp.request.resource_type
            except Exception:
                pass
            if rtype in ("xhr", "fetch", "websocket", "document", "eventsource"):
                return
            if resp.status >= 400:
                return
            body = resp.body()
            if not body or len(body) > MAX_ASSET_BYTES:
                return
            if total_bytes_ref[0] + len(body) > MAX_TOTAL_ASSET_BYTES:
                return
            ct = ""
            try:
                ct = (resp.header_value("content-type") or "").split(";")[0].strip().lower()
            except Exception:
                pass
            ext = CT_EXT_MAP.get(ct)
            if not ext:
                path_ext = Path(urllib.parse.urlparse(url).path).suffix.lower()
                ext = path_ext if len(path_ext) <= 6 else ""
            fname = hashlib.sha1(url.encode("utf-8")).hexdigest()[:16] + (ext or ".bin")
            (asset_dir / fname).write_bytes(body)
            total_bytes_ref[0] += len(body)
            url_map[url] = f"assets/{fname}"
        except Exception:
            pass  # capturing is best-effort

    def _rewrite_html(self, html: str, url_map: Dict[str, str]) -> str:
        for url, rel in url_map.items():
            if url in html:
                html = html.replace(url, rel)
            escaped = url.replace("&", "&amp;")
            if escaped != url and escaped in html:
                html = html.replace(escaped, rel.replace("&", "&amp;"))
        return html

    # ------------------------------------------------------------------
    # Download handling
    # ------------------------------------------------------------------
    def _save_download(self, download, path_ref, name_ref):
        try:
            suggested = download.suggested_filename or "download.bin"
            dest_dir = Path(self.config.downloads_dir)
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / suggested
            i = 1
            while dest.exists():
                dest = dest_dir / f"{Path(suggested).stem}_{i}{Path(suggested).suffix}"
                i += 1
            download.save_as(str(dest))
            path_ref[0] = dest
            name_ref[0] = suggested
        except Exception as e:
            logger.warning(f"Download save failed: {e}")

    def _maybe_file_result(self, page, main_response, path_ref, name_ref, update_status):
        if path_ref[0]:
            update_status(f"فایل دانلود شد: {name_ref[0]}")
            p = path_ref[0]
            return FetchResult(
                success=True,
                response_type="file",
                downloaded_file_path=str(p),
                meta={"download_filename": name_ref[0], "size": p.stat().st_size, "engine": "local"},
            )
        if main_response is not None:
            try:
                cd = main_response.header_value("content-disposition") or ""
                if "attachment" in cd.lower():
                    body = main_response.body()
                    filename = None
                    for part in cd.split(";"):
                        part = part.strip()
                        if part.lower().startswith("filename="):
                            filename = part.split("=", 1)[1].strip().strip('"')
                    if not filename:
                        filename = Path(urllib.parse.urlparse(main_response.url).path).name or "download.bin"
                    dest_dir = Path(self.config.downloads_dir)
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    dest = dest_dir / filename
                    dest.write_bytes(body)
                    update_status(f"فایل دانلود شد: {filename}")
                    return FetchResult(
                        success=True,
                        response_type="file",
                        downloaded_file_path=str(dest),
                        meta={"download_filename": filename, "size": len(body), "sha256": hashlib.sha256(body).hexdigest(), "engine": "local"},
                    )
            except Exception as e:
                logger.debug(f"Attachment check failed: {e}")
        return None
