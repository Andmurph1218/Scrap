"""Sessão Playwright autenticada na Wago."""

from __future__ import annotations

from playwright.sync_api import Browser, BrowserContext, Page, Playwright

from wago.config import AUTH_STATE, WAGO_HOME, WAGO_LOGIN, env, require_env


def login_wago(page: Page) -> None:
    require_env("WAGO_USER", "WAGO_PASS")
    user = env("WAGO_USER")
    password = env("WAGO_PASS")

    print("Realizando login na Wago...")
    page.goto(WAGO_LOGIN, timeout=60000)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)

    user_input = page.locator(
        'input[type="email"], input[name="username"], input[name="email"],'
        ' input[type="text"]'
    ).first
    pass_input = page.locator('input[type="password"]').first

    user_input.fill(user or "")
    pass_input.fill(password or "")

    try:
        page.locator(
            'button:has-text("Login"), button:has-text("Entrar"),'
            ' button:has-text("Acessar"), input[type="submit"]'
        ).first.click(timeout=15000)
    except Exception:
        pass_input.press("Enter")

    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(4000)
    print("Login realizado.")


def preparar_navegador(
    playwright: Playwright, *, headless: bool = True
) -> tuple[Browser, BrowserContext, Page]:
    """Inicia Chromium headless e autentica se necessário."""
    require_env("WAGO_USER", "WAGO_PASS")

    browser = playwright.chromium.launch(headless=headless)
    storage = str(AUTH_STATE) if AUTH_STATE.exists() else None
    context = browser.new_context(storage_state=storage)
    page = context.new_page()

    page.goto(WAGO_HOME, timeout=60000)
    page.wait_for_load_state("domcontentloaded", timeout=30000)
    page.wait_for_timeout(2000)

    precisa_login = False
    try:
        login_btn = page.locator(
            'a[href*="login"], button:has-text("Login"), a:has-text("Login")'
        ).first
        if login_btn.is_visible(timeout=5000):
            precisa_login = True
    except Exception:
        # Sem botão de login — assume sessão ativa se storage existir.
        precisa_login = not AUTH_STATE.exists()

    if precisa_login:
        login_wago(page)
        page.wait_for_timeout(2000)

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
