"""Extração de dados da página de produto (preço, IPI, specs, fotos)."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

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


def limpar_carrinho(page: Page, bearer: str | None = None) -> None:
    """
    Esvazia o carrinho.

    Preferência: botão "Redefinir carrinho" + confirmação do modal.
    Fallback: remove entradas via API OCC com Bearer.
    """
    bearer = bearer or _obter_bearer(page)

    # 1) UI — data-testid="clear-cart-btn"
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
            page.wait_for_timeout(800)
            modal = page.locator('[data-testid="wg-modal"]')
            if modal.count():
                for sel in (
                    'button:has-text("Remover")',
                    'button:has-text("Confirmar")',
                    'button:has-text("Sim")',
                    "button.w-button--primary",
                ):
                    btn = modal.locator(sel)
                    if btn.count():
                        btn.last.click(timeout=5000)
                        page.wait_for_timeout(1500)
                        break
            return
    except Exception:
        pass

    # 2) API — apaga cada entry
    if not bearer:
        return
    try:
        cart = _get_cart(page, bearer)
        for entry in cart.get("entries") or []:
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
    except Exception:
        pass


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


def capturar_ipi_carrinho(page: Page, sku: str) -> float:
    """
    Captura IPI unitário via API OCC autenticada (Bearer).

    Fluxo: limpa carrinho → addall → GET cart FULL → tax IPI / qty → limpa.
    """
    bearer = _obter_bearer(page)
    if not bearer:
        print("  Aviso: Bearer Wago não capturado; IPI=0.")
        return 0.0

    limpar_carrinho(page, bearer=bearer)

    # Add — mesmo payload do storefront
    try:
        res = page.request.post(
            f"{CART_API}/addall?lang=pt_BR",
            headers=_cart_headers(bearer),
            data=json.dumps(
                {
                    "orderEntries": [
                        {"product": {"code": sku}, "quantity": 1}
                    ]
                }
            ),
            timeout=30000,
        )
        if not res.ok:
            # UI fallback
            page.goto(f"https://www.wago.com/br/p/{sku}", timeout=60000)
            page.wait_for_timeout(2000)
            btn = page.locator(
                'button:has-text("Adicionar ao carrinho")'
            ).first
            btn.click(timeout=10000)
            page.wait_for_timeout(2500)
    except Exception:
        return 0.0

    unit_ipi = 0.0
    try:
        cart = _get_cart(page, bearer)
        sku_u = sku.strip().upper()
        for entry in cart.get("entries") or []:
            code = str((entry.get("product") or {}).get("code") or "").upper()
            if code == sku_u:
                val = _ipi_unitario_da_entry(entry)
                if val is not None:
                    unit_ipi = val
                break
        else:
            # HTML fallback se API não listar o SKU
            page.goto(WAGO_CART, timeout=60000)
            try:
                page.wait_for_selector("text=Item n", timeout=15000)
            except Exception:
                pass
            texto = page.inner_text("body").replace("\xa0", " ")
            bloco = _bloco_item_carrinho(texto, sku)
            if bloco:
                linha_ipi, aliquota = _extrair_ipi_do_bloco(bloco)
                spu = _extrair_spu(bloco)
                if linha_ipi is not None and spu > 0:
                    unit_ipi = round(linha_ipi / spu, 2)
                elif aliquota is not None:
                    preco_u = _extrair_preco_unitario_bloco(bloco)
                    if preco_u is not None:
                        unit_ipi = round(preco_u * (aliquota / 100.0), 2)
    except Exception:
        unit_ipi = 0.0
    finally:
        limpar_carrinho(page, bearer=bearer)

    return unit_ipi


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

        val_ipi = capturar_ipi_carrinho(page, sku_real)
        divisor = margin_divisor()
        preco_venda = round((preco_custo + val_ipi) / divisor, 2)

        nome = sku_real
        h1 = soup.find("h1")
        if h1:
            nome = h1.get_text(strip=True)

        categorias_lista: list[str] = []
        breadcrumb = soup.find(class_=re.compile(r"breadcrumb", re.I))
        if breadcrumb:
            for item in breadcrumb.find_all(["li", "a"]):
                txt = re.sub(r"\s+", " ", item.get_text(strip=True))
                if (
                    txt
                    and txt.lower() not in ["início", "home", "wago"]
                    and txt.lower() != nome.lower()
                    and txt not in categorias_lista
                ):
                    categorias_lista.append(txt)
        categoria_final = " > ".join(categorias_lista)

        fotos: list[str] = []
        for img in soup.find_all("img"):
            src = img.get("src") or img.get("data-src")
            if src and "medias" in src and not str(src).endswith(".svg"):
                if not str(src).startswith("http"):
                    src = f"https://www.wago.com{src}"
                if src not in fotos:
                    fotos.append(str(src))

        specs_dict: dict[str, str] = {}
        peso_kg = altura_cm = largura_cm = comprimento_cm = ""

        elementos_tech = soup.find_all(
            ["table", "dl", "div"],
            class_=re.compile(
                r"spec|tech|parameter|properties|data-sheet|technical-data"
            ),
        )
        for container in elementos_tech:
            for row in container.find_all(
                ["tr", "div"], class_=re.compile(r"row|item")
            ):
                cols = [
                    c.text.strip()
                    for c in row.find_all(["td", "th", "dt", "dd"])
                    if c.text.strip()
                ]
                if len(cols) >= 2:
                    specs_dict[cols[0].rstrip(":")] = cols[1]

        for chave, valor in specs_dict.items():
            c_lower = chave.lower()
            v_num = re.search(r"[\d.,]+", valor)
            if not v_num:
                continue
            val_float = float(v_num.group(0).replace(",", "."))
            if "largura" in c_lower and not largura_cm:
                largura_cm = (
                    round(val_float / 10, 2) if "mm" in valor else val_float
                )
            elif "altura" in c_lower and not altura_cm:
                altura_cm = (
                    round(val_float / 10, 2) if "mm" in valor else val_float
                )
            elif (
                "profundidade" in c_lower or "comprimento" in c_lower
            ) and not comprimento_cm:
                comprimento_cm = (
                    round(val_float / 10, 2) if "mm" in valor else val_float
                )
            elif "peso" in c_lower and not peso_kg:
                peso_kg = (
                    round(val_float / 1000, 3)
                    if "g" in valor.lower()
                    else val_float
                )

        bloco_specs = "\n".join(f"• {k}: {v}" for k, v in specs_dict.items())
        if not bloco_specs:
            bloco_specs = f"• Código Wago: {sku_real}\n• Marca: Wago"

        descricao_completa = (
            f"Modelo: {nome or sku_real}\n\n"
            f"ESPECIFICAÇÕES TÉCNICAS E CARACTERÍSTICAS:\n"
            f"{bloco_specs}\n\n"
            f"PRODUTO ORIGINAL WAGO COM NOTA FISCAL E GARANTIA."
        ).strip()

        partes = re.split(r"[;,]", nome or sku_real)
        nome_curto = partes[0].strip()
        if nome_curto and nome_curto[0].islower():
            nome_curto = nome_curto[0].upper() + nome_curto[1:]

        titulo_anuncio = f"{nome_curto} {sku_real} Wago".strip()
        if len(titulo_anuncio) > 60:
            max_len_desc = 60 - len(sku_real) - len("Wago") - 2
            corte = nome_curto[:max_len_desc].rsplit(" ", 1)[0].strip()
            corte = re.sub(r"[^\w\s]$", "", corte).strip()
            titulo_anuncio = f"{corte} {sku_real} Wago".strip()

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
            "Código de barras": "",
            "Exibir na loja": "SIM",
            "Frete gratis": "NÃO",
            "Descrição": descricao_completa,
            "Tags": f"{sku_real}, Wago, {nome_curto}",
            "Título para SEO": titulo_anuncio,
            "Descrição para SEO": (
                descricao_completa[:157] + "..."
                if len(descricao_completa) > 160
                else descricao_completa
            ),
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
