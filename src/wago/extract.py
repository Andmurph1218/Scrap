"""Extração de dados da página de produto (preço, IPI, specs, fotos)."""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import Page

from wago.browser import obter_token_csrf
from wago.config import WAGO_CART, margin_divisor


def normalizar_slug(texto: str) -> str:
    texto = (
        unicodedata.normalize("NFD", str(texto))
        .encode("ascii", "ignore")
        .decode("utf-8")
    )
    texto = re.sub(r"[^\w\s-]", "", texto).strip().lower()
    return re.sub(r"[-\s]+", "-", texto)


def _parse_brl(raw: str) -> float | None:
    """Converte string monetária BR ou EN para float."""
    s = str(raw).strip()
    if not s:
        return None
    # "1.234,56" (BR) vs "1234.56" (EN)
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        val = float(s)
    except ValueError:
        return None
    if 0 < val < 100_000:
        return val
    return None


def _walk_jsonld(node: Any) -> list[dict]:
    if isinstance(node, list):
        out: list[dict] = []
        for item in node:
            out.extend(_walk_jsonld(item))
        return out
    if isinstance(node, dict):
        items = [node]
        if "@graph" in node:
            items.extend(_walk_jsonld(node["@graph"]))
        return items
    return []


def extrair_preco_custo(
    page: Page, soup: BeautifulSoup, body_text: str, sku: str
) -> float | None:
    """
    Preço autenticado com prioridade estável (P0).

    NÃO usa max() de todos os R$ da página — isso capturava frete/totais.
    Ordem: JSON-LD Product/Offer → data-price → JS price+BRL → bloco de preço.
    """
    # 1) JSON-LD Product > offers
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for item in _walk_jsonld(data):
            types = item.get("@type", "")
            type_list = types if isinstance(types, list) else [types]
            type_norm = {str(t).lower() for t in type_list}
            if not (type_norm & {"product", "offer", "aggregateoffer"}):
                # ainda assim tenta offers se existir
                if "offers" not in item and "price" not in item:
                    continue
            offers = item.get("offers")
            if offers is not None:
                if not isinstance(offers, list):
                    offers = [offers]
                for offer in offers:
                    if not isinstance(offer, dict):
                        continue
                    price = offer.get("price") or offer.get("lowPrice")
                    if price is not None:
                        val = _parse_brl(str(price))
                        if val is not None:
                            return val
            if "price" in item:
                val = _parse_brl(str(item["price"]))
                if val is not None:
                    return val

    html = page.content()

    # 2) data-price / data-product-price
    for pattern in (
        r'data-(?:product-)?price=["\']([\d.,]+)["\']',
        r'data-price-value=["\']([\d.,]+)["\']',
    ):
        match = re.search(pattern, html, re.I)
        if match:
            val = _parse_brl(match.group(1))
            if val is not None:
                return val

    # 3) JSON embutido com currency BRL + price
    for pattern in (
        r'"price"\s*:\s*"?([\d.,]+)"?\s*,\s*"currency"\s*:\s*"BRL"',
        r'"currency"\s*:\s*"BRL"\s*,\s*"price"\s*:\s*"?([\d.,]+)"?',
        rf'{re.escape(sku)}.{{0,400}}"price"\s*:\s*"?([\d.,]+)"?',
    ):
        match = re.search(pattern, html, re.I | re.DOTALL)
        if match:
            val = _parse_brl(match.group(1))
            if val is not None:
                return val

    # 4) Bloco de preço autenticado (seletores comuns Hybris/SAP)
    for selector in (
        ".price",
        ".product-price",
        "[class*='price']",
        "[itemprop='price']",
    ):
        try:
            for el in soup.select(selector):
                text = el.get_text(" ", strip=True)
                # ignora faixas/listas longas
                if len(text) > 80:
                    continue
                match = re.search(r"R\$\s*([\d.,]+)", text)
                if match:
                    val = _parse_brl(match.group(1))
                    if val is not None:
                        return val
                content = el.get("content") or el.get("data-price")
                if content:
                    val = _parse_brl(str(content))
                    if val is not None:
                        return val
        except Exception:
            continue

    # 5) Último recurso: primeiro R$ após a palavra "preço"/"custo" no texto
    match = re.search(
        r"(?:preço|preco|custo|seu\s+preço)[^R$]{0,40}R\$\s*([\d.,]+)",
        body_text,
        re.I,
    )
    if match:
        return _parse_brl(match.group(1))

    return None


def _parse_valor_ipi(texto: str) -> float | None:
    """
    Extrai valor monetário de IPI.

    Formato real do carrinho Wago BR:
      IPI 9.75% (à incluir) R$ 349,83
    Evita o falso positivo em "Valor total sem IPI R$ …".
    """
    for regex in (
        # Linha do item: IPI 9.75% (à incluir) R$ 349,83
        r"IPI\s*[\d]+(?:[.,]\d+)?\s*%\s*\([^)]*\)\s*R\$\s*([\d.]+,\d{2}|\d+)",
        # Resumo: rótulo IPI isolado + valor (não "sem IPI")
        r"(?<!sem\s)(?<!com\s)IPI\s*(?:Total)?\s*R\$\s*([\d.]+,\d{2}|\d+)",
        r"R\$\s*([\d.]+,\d{2}|\d+)\s*(?:de\s*)?IPI\b",
    ):
        match = re.search(regex, texto, re.I)
        if match:
            return _parse_brl(match.group(1))
    return None


def _bloco_item_carrinho(texto: str, sku: str) -> str | None:
    """Recorta o bloco de texto do item no carrinho pelo SKU."""
    sku_u = sku.strip().upper()
    # Aceita "Item n.º 221-413" / "Item n.o" / SKU cru
    pattern = re.compile(
        rf"(?:Item\s*n[.ºo°]*\s*)?{re.escape(sku_u)}\b",
        re.I,
    )
    match = pattern.search(texto)
    if not match:
        # tenta sem hífens
        sku_compact = sku_u.replace("-", "")
        pattern = re.compile(
            rf"(?:Item\s*n[.ºo°]*\s*)?{re.escape(sku_compact)}\b",
            re.I,
        )
        match = pattern.search(texto.replace("-", ""))
        if not match:
            return None
        # fallback: janela ao redor do match no texto original
        idx = texto.upper().find(sku_u)
        if idx < 0:
            return None
        return texto[max(0, idx - 80) : idx + 900]

    start = match.start()
    # Bloco até o próximo item ou fim
    rest = texto[start:]
    next_item = re.search(r"\bItem\s*n[.ºo°]*\s*\d", rest[len(match.group(0)) :], re.I)
    if next_item:
        end = len(match.group(0)) + next_item.start()
        return rest[:end]
    return rest[:1200]


def _extrair_spu(bloco: str) -> int:
    """PU/SPU = embalagem mínima (qty=1 no carrinho pode ser 100/500 peças)."""
    match = re.search(
        r"PU\s*\(\s*SPU\s*\)\s*:\s*(\d+)",
        bloco,
        re.I,
    )
    if match:
        return max(int(match.group(1)), 1)
    match = re.search(r"SPU\s*:\s*(\d+)", bloco, re.I)
    if match:
        return max(int(match.group(1)), 1)
    return 1


def _extrair_ipi_do_bloco(bloco: str) -> tuple[float | None, float | None]:
    """
    Retorna (valor_ipi_linha, aliquota_percent) a partir do bloco do item.
    Ex.: IPI 9.75% (à incluir) R$ 349,83 → (349.83, 9.75)
    """
    match = re.search(
        r"IPI\s*([\d]+(?:[.,]\d+)?)\s*%\s*\([^)]*\)\s*R\$\s*([\d.]+,\d{2}|\d+)",
        bloco,
        re.I,
    )
    if match:
        aliq_raw = match.group(1).replace(",", ".")
        try:
            aliquota = float(aliq_raw)
        except ValueError:
            aliquota = None
        return _parse_brl(match.group(2)), aliquota

    valor = _parse_valor_ipi(bloco)
    aliq_m = re.search(r"IPI\s*([\d]+(?:[.,]\d+)?)\s*%", bloco, re.I)
    aliquota = None
    if aliq_m:
        try:
            aliquota = float(aliq_m.group(1).replace(",", "."))
        except ValueError:
            aliquota = None
    return valor, aliquota


def _extrair_preco_unitario_bloco(bloco: str) -> float | None:
    match = re.search(
        r"Seu\s+preço\s+por\s+unidade\s*R\$\s*([\d.]+,\d{2}|\d+)",
        bloco,
        re.I,
    )
    if match:
        return _parse_brl(match.group(1))
    return None


def _extrair_quantidade_carrinho(
    soup: BeautifulSoup, sku: str, texto: str
) -> int:
    """Quantidade de peças do item (prefere SPU do bloco do SKU)."""
    bloco = _bloco_item_carrinho(texto, sku)
    if bloco:
        return _extrair_spu(bloco)

    sku_norm = sku.replace("-", "").replace("_", "").upper()
    for tag in soup.find_all(["tr", "div", "li"]):
        tag_text = tag.get_text(" ", strip=True).upper()
        if sku.upper() in tag_text or sku_norm in tag_text.replace(
            "-", ""
        ).replace("_", ""):
            spu = _extrair_spu(tag_text)
            if spu > 1:
                return spu
            qty_input = tag.find(
                "input", attrs={"name": re.compile(r"qty|quantity", re.I)}
            )
            if qty_input:
                try:
                    return int(qty_input.get("value", "1"))
                except (ValueError, TypeError):
                    pass
    return 1


CART_API = (
    "https://www.wago.com/wagoapi/v2/br-wago/users/current/carts/current"
)


def _obter_bearer(page: Page) -> str | None:
    """Captura o Authorization Bearer das chamadas autenticadas do storefront."""
    holder: dict[str, str | None] = {"t": None}

    def on_req(req) -> None:
        if holder["t"]:
            return
        auth = req.headers.get("authorization")
        if auth and auth.lower().startswith("bearer "):
            holder["t"] = auth

    page.on("request", on_req)
    try:
        # Qualquer navegação autenticada dispara /carts/current/entries/count
        if "wago.com" not in (page.url or ""):
            page.goto("https://www.wago.com/br", timeout=60000)
        page.wait_for_timeout(1500)
        if not holder["t"]:
            try:
                page.goto(WAGO_CART, timeout=60000)
                page.wait_for_timeout(2500)
            except Exception:
                pass
        if not holder["t"]:
            # Força um hit leve via UI (botão do header do carrinho)
            try:
                page.locator('a[href*="/cart"]').first.click(timeout=5000)
                page.wait_for_timeout(2000)
            except Exception:
                pass
    finally:
        try:
            page.remove_listener("request", on_req)
        except Exception:
            pass
    return holder["t"]


def _cart_headers(bearer: str) -> dict[str, str]:
    return {
        "Authorization": bearer,
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _get_cart(page: Page, bearer: str) -> dict[str, Any]:
    res = page.request.get(
        f"{CART_API}?lang=pt_BR&fields=FULL",
        headers=_cart_headers(bearer),
        timeout=30000,
    )
    if not res.ok:
        return {}
    data = res.json()
    return data.get("cart") or data


def _limpar_carrinho_api(page: Page, bearer: str) -> bool:
    """Tenta esvaziar o carrinho via API OCC e retorna True se ficar vazio."""
    try:
        page.request.delete(
            f"{CART_API}/removeEntries?lang=pt_BR",
            headers=_cart_headers(bearer),
            timeout=15000,
        )
    except Exception:
        pass

    try:
        cart = _get_cart(page, bearer)
        entries = cart.get("entries") or []
        if not entries:
            return True
        for entry in sorted(
            entries,
            key=lambda e: int(e.get("entryNumber") or 0),
            reverse=True,
        ):
            num = entry.get("entryNumber")
            if num is None:
                continue
            try:
                page.request.delete(
                    f"{CART_API}/entries/{num}?lang=pt_BR",
                    headers=_cart_headers(bearer),
                    timeout=15000,
                )
            except Exception:
                continue
        cart_pos = _get_cart(page, bearer)
        return not (cart_pos.get("entries") or [])
    except Exception:
        return False


def limpar_carrinho(page: Page, bearer: str | None = None) -> None:
    """
    Esvazia o carrinho Wago.

    1) Tenta endpoint oficial DELETE /carts/current/removeEntries via API OCC.
    2) Fallback UI: botão "Redefinir carrinho" + confirmação "Reset"
       ([data-testid="clear-cart-modal-btn"]).
    """
    bearer = bearer or _obter_bearer(page)
    if bearer and _limpar_carrinho_api(page, bearer):
        return

    # Fallback UI — data-testid="clear-cart-btn" + modal clear-cart-modal-btn
    try:
        page.goto(WAGO_CART, timeout=60000)
        page.wait_for_timeout(2000)
        clear_btn = page.locator('[data-testid="clear-cart-btn"]')
        if clear_btn.count() == 0:
            clear_btn = page.locator(
                'button:has-text("Redefinir carrinho"), a:has-text("Redefinir carrinho")'
            )
        if clear_btn.count() and clear_btn.first.is_visible(timeout=3000):
            clear_btn.first.click(timeout=5000)
            page.wait_for_timeout(1000)
            for sel in (
                '[data-testid="clear-cart-modal-btn"]',
                'button:has-text("Reset")',
                'button:has-text("Redefinir")',
                'button:has-text("Remover")',
                'button:has-text("Confirmar")',
                'button:has-text("Sim")',
                "button.w-button--error",
            ):
                btn = page.locator(sel)
                if btn.count() and btn.first.is_visible(timeout=1500):
                    btn.first.click(timeout=5000)
                    page.wait_for_timeout(1500)
                    break
    except Exception:
        pass

    if bearer:
        _limpar_carrinho_api(page, bearer)


def _ipi_unitario_da_entry(entry: dict[str, Any]) -> float | None:
    """IPI unitário = tax BX23 (label IPI…) / quantity."""
    qty = int(entry.get("quantity") or 0)
    if qty <= 0:
        return None
    for tax in entry.get("taxes") or []:
        label = str(tax.get("label") or "")
        code = str(tax.get("code") or "")
        if code == "BX23" or re.search(r"\bIPI\b", label, re.I):
            value = (tax.get("value") or {}).get("value")
            if value is None:
                continue
            try:
                return round(float(value) / qty, 2)
            except (TypeError, ValueError):
                continue
    # Fallback: basePrice × alíquota do label "IPI 9.75%"
    base = (entry.get("basePrice") or {}).get("value")
    for tax in entry.get("taxes") or []:
        label = str(tax.get("label") or "")
        m = re.search(r"IPI\s*([\d]+(?:[.,]\d+)?)\s*%", label, re.I)
        if m and base is not None:
            try:
                aliq = float(m.group(1).replace(",", "."))
                return round(float(base) * (aliq / 100.0), 2)
            except (TypeError, ValueError):
                pass
    return None


@dataclass(frozen=True)
class ResultadoIpi:
    """Consulta de IPI. `ok` com valor 0.0 é IPI real; `ok` falso não tem valor."""

    ok: bool
    valor: float | None = None
    motivo: str = ""


def preco_com_ipi(
    custo: float, ipi: ResultadoIpi, divisor: float | None = None
) -> float | None:
    """Preço de venda. Não calcula quando a consulta de IPI falhou."""
    if not ipi.ok or ipi.valor is None:
        return None
    div = margin_divisor() if divisor is None else divisor
    if div <= 0:
        return None
    return round((float(custo) + float(ipi.valor)) / div, 2)


def _ler_carrinho(page: Page, bearer: str) -> tuple[dict[str, Any] | None, str]:
    try:
        res = page.request.get(
            f"{CART_API}?lang=pt_BR&fields=FULL",
            headers=_cart_headers(bearer),
            timeout=30000,
        )
    except Exception as exc:
        return None, f"falha ao ler carrinho: {exc}"
    if not getattr(res, "ok", False):
        status = getattr(res, "status", None)
        if not isinstance(status, int):
            status = "?"
        return None, f"carrinho HTTP {status}"
    try:
        data = res.json()
    except Exception as exc:
        return None, f"JSON do carrinho inválido: {exc}"
    if not isinstance(data, dict):
        return None, "carrinho em formato inválido"
    cart = data.get("cart") or data
    if not isinstance(cart, dict):
        return None, "carrinho em formato inválido"
    return cart, ""


def _ipi_pelo_html(page: Page, sku: str) -> ResultadoIpi | None:
    try:
        page.goto(WAGO_CART, timeout=60000)
        try:
            page.wait_for_selector("text=Item n", timeout=15000)
        except Exception:
            pass
        texto = page.inner_text("body").replace("\xa0", " ")
    except Exception:
        return None
    bloco = _bloco_item_carrinho(texto, sku)
    if not bloco:
        return None
    linha_ipi, aliquota = _extrair_ipi_do_bloco(bloco)
    spu = _extrair_spu(bloco)
    if linha_ipi is not None and spu > 0:
        return ResultadoIpi(
            ok=True, valor=round(linha_ipi / spu, 2), motivo="html do carrinho"
        )
    if aliquota is not None:
        preco_u = _extrair_preco_unitario_bloco(bloco)
        if preco_u is not None:
            return ResultadoIpi(
                ok=True,
                valor=round(preco_u * (aliquota / 100.0), 2),
                motivo="html do carrinho",
            )
    return None


def _norm_sku(sku: str) -> str:
    return re.sub(r"[_/]", "-", str(sku or "").strip().upper())


def _adicionar_ao_carrinho(page: Page, bearer: str, sku: str) -> str | None:
    """Devolve o motivo da falha, ou None se o POST/UI não lançou erro."""
    codigos_tentativa = [sku]
    if "_" in sku:
        codigos_tentativa.append(sku.replace("_", "/"))
    try:
        for codigo in codigos_tentativa:
            res = page.request.post(
                f"{CART_API}/addall?lang=pt_BR",
                headers=_cart_headers(bearer),
                data=json.dumps(
                    {"orderEntries": [{"product": {"code": codigo}, "quantity": 1}]}
                ),
                timeout=30000,
            )
            if getattr(res, "ok", False):
                return None
        page.goto(f"https://www.wago.com/br/p/{sku}", timeout=60000)
        page.wait_for_timeout(2000)
        btn = page.locator('button:has-text("Adicionar ao carrinho")').first
        btn.click(timeout=10000)
        page.wait_for_timeout(2500)
    except Exception as exc:
        return f"falha ao adicionar ao carrinho: {exc}"
    return None


def _ipi_da_entrada(entry: dict[str, Any]) -> ResultadoIpi:
    val = _ipi_unitario_da_entry(entry)
    if val is None:
        return ResultadoIpi(
            ok=True,
            valor=0.0,
            motivo="entrada sem tributo IPI; tratado como zero",
        )
    if val == 0:
        return ResultadoIpi(ok=True, valor=0.0, motivo="ipi igual a zero")
    return ResultadoIpi(ok=True, valor=val, motivo="api occ")


def capturar_ipi_carrinho(page: Page, sku: str) -> ResultadoIpi:
    """
    Captura IPI unitário via API OCC autenticada (Bearer).

    Fluxo: limpa carrinho → addall → GET cart FULL → tax IPI / qty → limpa.
    Falha de consulta não vira 0.0. IPI medido como zero continua válido.
    """
    bearer = _obter_bearer(page)
    if not bearer:
        return ResultadoIpi(ok=False, motivo="bearer ausente")

    limpar_carrinho(page, bearer=bearer)
    try:
        falha_add = _adicionar_ao_carrinho(page, bearer, sku)
        if falha_add:
            return ResultadoIpi(ok=False, motivo=falha_add)

        cart, erro_cart = _ler_carrinho(page, bearer)
        sku_norm = _norm_sku(sku)
        if cart is not None:
            for entry in cart.get("entries") or []:
                code = str((entry.get("product") or {}).get("code") or "").upper()
                if _norm_sku(code) == sku_norm:
                    return _ipi_da_entrada(entry)

        html = _ipi_pelo_html(page, sku)
        if html is not None:
            return html
        if erro_cart:
            return ResultadoIpi(ok=False, motivo=erro_cart)
        return ResultadoIpi(ok=False, motivo="SKU ausente no carrinho")
    finally:
        limpar_carrinho(page, bearer=bearer)


WAGO_PRODUCT_API = "https://www.wago.com/wagoapi/v2/br-wago/products"


def _limpar_html_texto(raw: str) -> str:
    if not raw:
        return ""
    s = re.sub(r"<br\s*/?>", "\n", str(raw), flags=re.I)
    s = re.sub(r"</p\s*>", "\n", s, flags=re.I)
    s = re.sub(r"<li[^>]*>", "• ", s, flags=re.I)
    s = re.sub(r"</li\s*>", "\n", s, flags=re.I)
    soup = BeautifulSoup(s, "html.parser")
    linhas = [
        re.sub(r"[ \t\xa0]+", " ", ln).strip()
        for ln in soup.get_text("\n").splitlines()
    ]
    return "\n".join(ln for ln in linhas if ln)


def _valor_feature_wago(feat: dict[str, Any]) -> str:
    unit = (feat.get("featureUnit") or {}).get("symbol") or (
        feat.get("featureUnit") or {}
    ).get("name") or ""
    unit = str(unit).strip()
    partes: list[str] = []
    for fv in feat.get("featureValues") or []:
        val = _limpar_html_texto(str(fv.get("value") or ""))
        if not val:
            continue
        if unit and not val.lower().endswith(unit.lower()):
            val = f"{val} {unit}"
        partes.append(val)
    return "; ".join(partes)


def _parse_grupos_classifications(
    grupos: list[dict[str, Any]],
) -> tuple[dict[str, str], list[tuple[str, list[tuple[str, str]]]]]:
    """Extrai pares (chave, valor) e seções ordenadas (Observações, Dados Elétricos, etc.)."""
    specs_dict: dict[str, str] = {}
    secoes: list[tuple[str, list[tuple[str, str]]]] = []

    def _coletar_features(
        bloco: dict[str, Any], destino: list[tuple[str, str]]
    ) -> None:
        for feat in bloco.get("features") or []:
            chave = str(feat.get("name") or "").strip().rstrip(":")
            valor = _valor_feature_wago(feat)
            if not chave or not valor:
                continue
            destino.append((chave, valor))
            specs_dict.setdefault(chave, valor)
        for sub in bloco.get("subClassifications") or []:
            _coletar_features(sub, destino)

    for grp in grupos or []:
        if not isinstance(grp, dict):
            continue
        titulo = str(grp.get("name") or "").strip() or "Especificações"
        itens: list[tuple[str, str]] = []
        _coletar_features(grp, itens)
        if itens:
            secoes.append((titulo, itens))

    return specs_dict, secoes


def _interpretar_medidas_e_codigos(specs_dict: dict[str, str]) -> dict[str, Any]:
    peso_kg: Any = ""
    altura_cm: Any = ""
    largura_cm: Any = ""
    comprimento_cm: Any = ""
    gtin = ""
    ncm = ""

    def _num_cm(valor: str) -> float | None:
        v_num = re.search(r"[\d.,]+", valor)
        if not v_num:
            return None
        try:
            val_float = float(v_num.group(0).replace(",", "."))
        except ValueError:
            return None
        if val_float <= 0:
            return None
        return round(val_float / 10, 2) if "mm" in valor.lower() else round(val_float, 2)

    for chave, valor in specs_dict.items():
        c_lower = chave.lower().strip()
        if c_lower in {"gtin", "ean", "código de barras", "codigo de barras"}:
            m_gtin = re.search(r"\b(\d{8,14})\b", valor)
            if m_gtin and not gtin:
                gtin = m_gtin.group(1)
        if (
            "tarifa alfandegária" in c_lower
            or "tarifa alfandegaria" in c_lower
            or c_lower == "ncm"
        ):
            m_ncm = re.search(r"(\d{4}\.\d{2}\.\d{2}|\d{8})", valor)
            if m_ncm and not ncm:
                ncm = m_ncm.group(1)
        if c_lower == "largura" and not largura_cm:
            largura_cm = _num_cm(valor) or ""
        elif c_lower == "altura" and not altura_cm:
            altura_cm = _num_cm(valor) or ""
        elif c_lower in {"profundidade", "comprimento"} and not comprimento_cm:
            comprimento_cm = _num_cm(valor) or ""
        elif (c_lower == "peso" or "peso" in c_lower) and not peso_kg:
            v_num = re.search(r"[\d.,]+", valor)
            if v_num:
                try:
                    val_float = float(v_num.group(0).replace(",", "."))
                except ValueError:
                    val_float = 0.0
                if val_float > 0:
                    peso_bruto = (
                        round(val_float / 1000, 3)
                        if "g" in valor.lower() and "kg" not in valor.lower()
                        else round(val_float, 3)
                    )
                    peso_kg = max(peso_bruto, 0.001)

    for chave, valor in specs_dict.items():
        c_lower = chave.lower().strip()
        if not largura_cm and (
            (
                "largura" in c_lower
                and "marcador" not in c_lower
                and "marcação" not in c_lower
                and "marcacao" not in c_lower
                and "desencape" not in c_lower
            )
            or c_lower in {"diâmetro", "diametro", "diâmetro 1", "diametro 1"}
        ):
            largura_cm = _num_cm(valor) or ""
        if not altura_cm and (
            "altura" in c_lower
            or c_lower in {"diâmetro 2", "diametro 2", "diâmetro 1", "diametro 1"}
        ):
            altura_cm = _num_cm(valor) or ""
        if not comprimento_cm and (
            (
                ("profundidade" in c_lower or "comprimento" in c_lower)
                and "desencape" not in c_lower
                and "trilho" not in c_lower
            )
            or "total length" in c_lower
        ):
            comprimento_cm = _num_cm(valor) or ""

    return {
        "peso_kg": peso_kg,
        "altura_cm": altura_cm,
        "largura_cm": largura_cm,
        "comprimento_cm": comprimento_cm,
        "gtin": gtin,
        "ncm": ncm,
    }


def _extrair_specs_dom(soup: BeautifulSoup) -> dict[str, Any]:
    """Lê a tabela de especificações técnicas da página Wago."""
    specs_dict: dict[str, str] = {}
    secoes: list[tuple[str, list[tuple[str, str]]]] = []

    accordions = [
        acc
        for acc in soup.select(".w-accordion")
        if acc.find_parent(class_="w-accordion") is None
    ]
    for acc in accordions:
        t_el = acc.find(class_="w-typography")
        titulo = (
            re.sub(r"\s+", " ", t_el.get_text(" ", strip=True)).strip()
            if t_el
            else "Especificações"
        )
        itens: list[tuple[str, str]] = []
        for row in acc.find_all("tr"):
            cols = [
                re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
                for c in row.find_all(["td", "th"], recursive=False)
                if c.get_text(strip=True)
            ]
            if len(cols) >= 2:
                chave = cols[0].rstrip(":").strip()
                if chave:
                    itens.append((chave, cols[1]))
                    specs_dict.setdefault(chave, cols[1])
        if itens:
            secoes.append((titulo, itens))

    for row in soup.find_all(["tr", "div"]):
        cols = [
            re.sub(r"\s+", " ", c.get_text(" ", strip=True)).strip()
            for c in row.find_all(["td", "th", "dt", "dd"], recursive=False)
            if c.get_text(strip=True)
        ]
        if len(cols) >= 2:
            chave = cols[0].rstrip(":").strip()
            if chave and chave not in specs_dict:
                specs_dict[chave] = cols[1]

    medidas = _interpretar_medidas_e_codigos(specs_dict)
    return {
        "specs": specs_dict,
        "secoes": secoes,
        **medidas,
    }


_TERMOS_BREADCRUMB_IGNORADOS = {
    "início",
    "inicio",
    "página inicial",
    "pagina inicial",
    "home",
    "wago",
    "produtos",
    "products",
    "...",
    "…",
    "geral",
    "product family",
}


def limpar_categoria_produto(raw_cat: str, sku: str = "", nome: str = "") -> str:
    """
    Retorna apenas a categoria principal em português, descartando ruído de breadcrumb,
    código de SKU e sufixos de família em inglês.
    """
    if not raw_cat:
        return ""
    sku_norm = _norm_sku(sku)
    nome_l = str(nome or "").strip().lower()
    partes = [re.sub(r"\s+", " ", p).strip() for p in str(raw_cat).split(">")]
    validas: list[str] = []
    for p in partes:
        if not p:
            continue
        pl = p.lower()
        if pl in _TERMOS_BREADCRUMB_IGNORADOS:
            continue
        if sku_norm and _norm_sku(p) == sku_norm:
            continue
        if re.fullmatch(r"\d{3,4}[-_/]\d{3,4}(?:[-_/]\d{3,4})?", p):
            continue
        if nome_l and pl == nome_l:
            continue
        if any(
            termo in pl
            for termo in (
                "marking system",
                "winsta",
                "topjob",
                "picomax",
                "cage clamp",
            )
        ):
            continue
        if p not in validas:
            validas.append(p)
    return validas[0] if validas else ""


def filtrar_melhores_fotos(urls: list[str] | str) -> list[str]:
    """
    Filtra URLs de fotos mantendo apenas a maior resolução de cada imagem única
    e descartando miniaturas (< 600px), ícones de UI (ex.: 0008.png) e duplicatas.
    """
    if isinstance(urls, str):
        brutas = [p.strip() for p in re.split(r"[\n,;]+", urls) if p.strip()]
    else:
        brutas = [str(u).strip() for u in (urls or []) if str(u).strip()]

    ordem_chaves: list[str] = []
    melhor_por_chave: dict[str, tuple[int, str]] = {}

    for u in brutas:
        if u.startswith("/medias/"):
            u = f"https://www.wago.com{u}"
        if not u.startswith("http"):
            continue
        parsed = urlparse(u)
        path = parsed.path or ""
        if path.lower().endswith(".svg"):
            continue
        filename = path.rstrip("/").split("/")[-1]
        if not filename or filename.lower() == "medias":
            continue
        # Ignora ícones pequenos de interface da Wago (0001.png, 0005.png, 0008.png, etc.)
        if re.fullmatch(r"0\d{3}\.png", filename, re.I):
            continue

        m = re.match(r"^(\d{2,4})-(.+)$", filename)
        if m:
            res = int(m.group(1))
            chave = m.group(2).lower()
            # Descarta miniaturas de baixa resolução (120px, 200px, 480px)
            if res < 600:
                continue
        else:
            res = 1000
            chave = path.lower()

        anterior = melhor_por_chave.get(chave)
        if anterior is None:
            ordem_chaves.append(chave)
            melhor_por_chave[chave] = (res, u)
        elif res > anterior[0]:
            melhor_por_chave[chave] = (res, u)

    return [melhor_por_chave[c][1] for c in ordem_chaves]


def _consultar_api_produto_wago(page: Page, sku: str) -> dict[str, Any]:
    """Busca dados estruturados e acordeões (Observações, Dados Elétricos, etc.) na API Wago."""
    info: dict[str, Any] = {
        "name": "",
        "short_name": "",
        "description": "",
        "categories": [],
        "images": [],
        "specs": {},
        "secoes": [],
        "peso_kg": "",
        "altura_cm": "",
        "largura_cm": "",
        "comprimento_cm": "",
        "gtin": "",
        "ncm": "",
    }
    try:
        res_p = page.request.get(
            f"{WAGO_PRODUCT_API}/{sku}?fields=FULL&lang=pt_BR",
            timeout=20000,
        )
        if getattr(res_p, "ok", False):
            prod = res_p.json()
            if isinstance(prod, dict):
                info["name"] = str(prod.get("name") or "").strip()
                info["short_name"] = str(prod.get("shortName") or "").strip()
                info["description"] = _limpar_html_texto(
                    str(prod.get("description") or "")
                )
                cats_principais: list[str] = []
                for cat_obj in prod.get("categories") or []:
                    if not isinstance(cat_obj, dict):
                        continue
                    code = str(cat_obj.get("code") or "").strip()
                    cname = limpar_categoria_produto(
                        str(cat_obj.get("name") or ""), sku=sku, nome=info["name"]
                    )
                    if code.startswith("10") and cname and cname not in cats_principais:
                        cats_principais.append(cname)
                if not cats_principais:
                    for c in prod.get("categoryNames") or []:
                        cname = limpar_categoria_produto(
                            str(c or ""), sku=sku, nome=info["name"]
                        )
                        if cname and cname not in cats_principais:
                            cats_principais.append(cname)
                info["categories"] = cats_principais[:1]
                urls_api = [
                    str(img.get("url") or "").strip()
                    for img in (prod.get("images") or [])
                    if isinstance(img, dict) and img.get("url")
                ]
                info["images"] = filtrar_melhores_fotos(urls_api)
    except Exception:
        pass

    try:
        res_c = page.request.get(
            f"{WAGO_PRODUCT_API}/{sku}/classifications?lang=pt_BR",
            timeout=20000,
        )
        if getattr(res_c, "ok", False):
            grupos = res_c.json()
            if isinstance(grupos, list):
                specs, secoes = _parse_grupos_classifications(grupos)
                info["specs"] = specs
                info["secoes"] = secoes
                info.update(_interpretar_medidas_e_codigos(specs))
    except Exception:
        pass
    return info


def montar_descricao_produto(
    *,
    sku: str,
    nome: str,
    resumo_curto: str = "",
    descricao_aba: str = "",
    secoes: list[tuple[str, list[tuple[str, str]]]] | None = None,
    specs_dict: dict[str, str] | None = None,
) -> str:
    """Monta apenas a descrição curta do item (sem tabelas de acordeões)."""
    texto_item = re.sub(r"\s+", " ", str(nome or resumo_curto or sku)).strip()
    linhas: list[str] = [texto_item]
    if sku:
        linhas.append(f"Código Wago: {sku} | Marca: Wago")
    linhas.append("Produto original Wago com nota fiscal e garantia.")
    return "\n".join(linhas).strip()


def extrair_produto(page: Page, url: str) -> dict[str, Any] | None:
    """Extrai um produto completo da URL /p/…"""
    from wago.discover import sku_from_url
    from wago.state import registrar_sku_sem_preco

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(2000)

        soup = BeautifulSoup(page.content(), "html.parser")
        body_text = page.inner_text("body")

        sku_real = sku_from_url(url)
        if not sku_real:
            return None

        preco_custo = extrair_preco_custo(page, soup, body_text, sku_real)
        if preco_custo is None:
            print(f"  SKU {sku_real} ignorado: sem preço de custo publicado.")
            registrar_sku_sem_preco(sku_real)
            return None

        ncm_real = ""
        ncm_match = re.search(
            r"NCM[\s:]*(\d{4}\.\d{2}\.\d{2}|\d{8})", body_text, re.I
        )
        if ncm_match:
            ncm_real = ncm_match.group(1)

        resultado_ipi = capturar_ipi_carrinho(page, sku_real)
        preco_venda = preco_com_ipi(preco_custo, resultado_ipi)
        if preco_venda is None:
            print(
                f"  SKU {sku_real} pendente: IPI indisponível "
                f"({resultado_ipi.motivo}). Preço não calculado."
            )
            return None
        if resultado_ipi.valor == 0:
            print(f"  SKU {sku_real}: IPI igual a zero ({resultado_ipi.motivo}).")
        val_ipi = resultado_ipi.valor

        nome = sku_real
        h1 = soup.find("h1")
        if h1:
            nome = h1.get_text(strip=True)

        categorias_dom: list[str] = []
        breadcrumb = soup.find(class_=re.compile(r"breadcrumb", re.I))
        if breadcrumb:
            for item in breadcrumb.find_all(["li", "a"]):
                txt = limpar_categoria_produto(
                    item.get_text(strip=True), sku=sku_real, nome=nome
                )
                if txt and txt not in categorias_dom:
                    categorias_dom.append(txt)

        fotos_dom: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src and "medias" in src and not str(src).endswith(".svg"):
                if not str(src).startswith("http"):
                    src = f"https://www.wago.com{src}"
                if src not in fotos_dom:
                    fotos_dom.append(str(src))

        dados_api = _consultar_api_produto_wago(page, sku_real)
        fotos = filtrar_melhores_fotos(
            (dados_api.get("images") or []) + fotos_dom
        )
        if dados_api["name"] and (not nome or nome == sku_real):
            nome = dados_api["name"]
        if dados_api["categories"]:
            categoria_final = dados_api["categories"][0]
        elif categorias_dom:
            categoria_final = categorias_dom[0]
        else:
            categoria_final = ""

        dados_specs = _extrair_specs_dom(soup)
        specs_dict = dict(dados_api["specs"])
        for k, v in dados_specs["specs"].items():
            specs_dict.setdefault(k, v)
        secoes = dados_api["secoes"] or dados_specs["secoes"]
        peso_kg = dados_api["peso_kg"] or dados_specs["peso_kg"]
        altura_cm = dados_api["altura_cm"] or dados_specs["altura_cm"]
        largura_cm = dados_api["largura_cm"] or dados_specs["largura_cm"]
        comprimento_cm = (
            dados_api["comprimento_cm"] or dados_specs["comprimento_cm"]
        )
        gtin_real = dados_api["gtin"] or dados_specs["gtin"]
        if not ncm_real:
            ncm_real = dados_api["ncm"] or dados_specs["ncm"]

        descricao_completa = montar_descricao_produto(
            sku=sku_real,
            nome=nome or sku_real,
            resumo_curto=dados_api["short_name"],
            descricao_aba=dados_api["description"],
            secoes=secoes,
            specs_dict=specs_dict,
        )

        partes = re.split(r"[;,]", dados_api["short_name"] or nome or sku_real)
        nome_curto = partes[0].strip()
        if nome_curto and nome_curto[0].islower():
            nome_curto = nome_curto[0].upper() + nome_curto[1:]

        titulo_anuncio = f"{nome_curto} {sku_real} Wago".strip()
        if len(titulo_anuncio) > 60:
            max_len_desc = 60 - len(sku_real) - len("Wago") - 2
            corte = nome_curto[:max_len_desc].rsplit(" ", 1)[0].strip()
            corte = re.sub(r"[^\w\s]$", "", corte).strip()
            titulo_anuncio = f"{corte} {sku_real} Wago".strip()

        desc_seo_base = re.sub(r"\s+", " ", descricao_completa).strip()
        descricao_seo = (
            desc_seo_base[:157].rstrip() + "..."
            if len(desc_seo_base) > 160
            else desc_seo_base
        )

        return {
            "Identificador URL": normalizar_slug(titulo_anuncio),
            "Nome": titulo_anuncio,
            "Categorias": categoria_final,
            "Nome da variação 1": "",
            "Valor da variação 1": "",
            "Nome da variação 2": "",
            "Valor da variação 2": "",
            "Nome da variação 3": "",
            "Valor da variação 3": "",
            "Preço": preco_venda,
            "Preço promocional": "",
            "Peso (kg)": peso_kg,
            "Altura (cm)": altura_cm,
            "Largura (cm)": largura_cm,
            "Comprimento (cm)": comprimento_cm,
            "Estoque": 2,
            "SKU": sku_real,
            "Código de barras": gtin_real,
            "Exibir na loja": "SIM",
            "Frete gratis": "NÃO",
            "Descrição": descricao_completa,
            "Tags": f"{sku_real}, Wago, {nome_curto}",
            "Título para SEO": titulo_anuncio,
            "Descrição para SEO": descricao_seo,
            "Marca": "Wago",
            "Produto Físico": "SIM",
            "MPN (Cód. Exclusivo Modelo Fabricante)": sku_real,
            "Sexo": "",
            "Faixa etária": "",
            "IPI": val_ipi,
            "NCM": ncm_real,
            "Fotos": ", ".join(fotos) if fotos else "",
            "Custo": preco_custo,
            "Visibilidade": "Visível",
        }
    except Exception as exc:
        print(f"Erro na URL {url}: {exc}")
        return None
