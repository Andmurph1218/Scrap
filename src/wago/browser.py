"""Sessão Playwright autenticada na Wago."""

from __future__ import annotations

import os
from pathlib import Path

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from wago.config import AUTH_STATE, WAGO_CART, WAGO_HOME, WAGO_LOGIN, env, require_env


def instalar_captura_bearer(page: Page) -> None:
    """Instala listeners na página para capturar sempre o commerce-token válido da Wago."""
    if getattr(page, "_wago_bearer_listener", False):
        return

    def _on_req(req) -> None:
        try:
            url = req.url or ""
            if "wagoapi/v2/" not in url:
                return
            auth = req.headers.get("authorization")
            if auth and auth.lower().startswith("bearer "):
                setattr(page, "_wago_bearer", auth)
        except Exception:
            pass

    def _on_resp(resp) -> None:
        try:
            url = resp.url or ""
            if "auth/commerce-token" in url and resp.status in (200, 201):
                dados = resp.json()
                tok = dados.get("access_token") if isinstance(dados, dict) else None
                if tok:
                    setattr(page, "_wago_bearer", f"Bearer {tok}")
        except Exception:
            pass

    try:
        page.on("request", _on_req)
        page.on("response", _on_resp)
        setattr(page, "_wago_bearer_listener", True)
    except Exception:
        pass


def _token_commerce_valido(page: Page) -> bool:
    bearer = getattr(page, "_wago_bearer", None)
    if not bearer or not isinstance(bearer, str):
        return False
    try:
        res = page.request.get(
            "https://www.wago.com/wagoapi/v2/br-wago/users/current/carts/current/entries/count?lang=pt_BR",
            headers={"Authorization": bearer, "Accept": "application/json"},
            timeout=15000,
        )
        return bool(getattr(res, "ok", False))
    except Exception:
        return False


def _tem_chromium(pasta: Path) -> bool:
    if not pasta.is_dir():
        return False
    return any(pasta.glob("chromium-*")) or any(
        pasta.glob("chromium_headless_shell-*")
    )


def garantir_navegador_playwright() -> None:
    """Usa o Chromium já instalado quando a pasta do processo está vazia."""
    atual = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if atual and _tem_chromium(Path(atual)):
        return
    local = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
    if _tem_chromium(local):
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(local)


def login_wago(page: Page) -> None:
    require_env("WAGO_USER", "WAGO_PASS")
    user = env("WAGO_USER")
    password = env("WAGO_PASS")

    instalar_captura_bearer(page)
    setattr(page, "_wago_bearer", None)

    print("Realizando login na Wago...")
    page.goto(WAGO_LOGIN, timeout=60000)
    try:
        page.wait_for_selector(
            '#email, input[type="email"], input[name="username"]',
            state="visible",
            timeout=30000,
        )
    except Exception:
        page.wait_for_timeout(2000)

    user_input = page.locator(
        '#email, input[type="email"], input[name="username"], input[name="email"]'
    ).first
    pass_input = page.locator('#password, input[type="password"]').first

    user_input.fill(user or "")
    pass_input.fill(password or "")

    try:
        page.locator(
            'button.wg-button--primary:has-text("Login"), '
            'button:has-text("Login"), button:has-text("Entrar"), '
            'button:has-text("Acessar"), input[type="submit"]'
        ).first.click(timeout=15000)
    except Exception:
        pass_input.press("Enter")

    # Aguarda o redirect SSO Gigya -> www.wago.com/br e a emissão do commerce-token
    for seg in range(25):
        page.wait_for_timeout(1000)
        url_atual = page.url or ""
        if (
            "www.wago.com/br" in url_atual
            and "gig_actions" not in url_atual
            and getattr(page, "_wago_bearer", None)
        ):
            break
        if seg == 14 and "www.wago.com/br" in url_atual and not getattr(page, "_wago_bearer", None):
            try:
                page.goto(WAGO_CART, timeout=45000)
            except Exception:
                pass

    if getattr(page, "_wago_bearer", None):
        print("Login realizado e token de preços validado.")
    else:
        print("Login realizado.")


def preparar_navegador(
    playwright: Playwright, *, headless: bool = True
) -> tuple[Browser, BrowserContext, Page]:
    """Inicia Chromium headless e autentica garantindo commerce-token ativo."""
    require_env("WAGO_USER", "WAGO_PASS")
    garantir_navegador_playwright()

    browser = playwright.chromium.launch(headless=headless)
    storage = str(AUTH_STATE) if AUTH_STATE.exists() else None
    context = browser.new_context(storage_state=storage)
    page = context.new_page()
    instalar_captura_bearer(page)

    page.goto(WAGO_HOME, timeout=60000)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    for _ in range(6):
        page.wait_for_timeout(1000)
        if getattr(page, "_wago_bearer", None):
            break

    if not _token_commerce_valido(page):
        login_wago(page)
        page.wait_for_timeout(1500)

    AUTH_STATE.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(AUTH_STATE))
    print(f"Estado de autenticação salvo em {AUTH_STATE}")
    return browser, context, page


def obter_token_csrf(page: Page) -> str:
    import re

    try:
        html = page.content()
        for pattern in (
            r'name="CSRFToken"[^>]*value="([^"]+)"',
            r'<meta[^>]*name="(?:CSRFToken|_csrf)"[^>]*content="([^"]+)"',
        ):
            match = re.search(pattern, html, re.I)
            if match:
                return match.group(1)

        res = page.request.get(WAGO_HOME, timeout=15000)
        if res.ok:
            text = res.text()
            for pattern in (
                r'name="CSRFToken"[^>]*value="([^"]+)"',
                r'<meta[^>]*name="(?:CSRFToken|_csrf)"[^>]*content="([^"]+)"',
            ):
                match = re.search(pattern, text, re.I)
                if match:
                    return match.group(1)
    except Exception:
        pass
    return ""
