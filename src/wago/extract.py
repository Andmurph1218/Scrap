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
    for regex in (
        r"IPI\s*(?:Total)?[\s:]*R\$\s*([\d.,]+)",
        r"R\$\s*([\d.,]+)\s*(?:de\s*)?IPI",
        r"IPI\s*R\$\s*([\d.,]+)",
    ):
        match = re.search(regex, texto, re.I)
        if match:
            return _parse_brl(match.group(1))
    return None


def _extrair_quantidade_carrinho(
    soup: BeautifulSoup, sku: str, texto: str
) -> int:
    sku_norm = sku.replace("-", "").replace("_", "").upper()
    for tag in soup.find_all(["tr", "div", "li"]):
        tag_text = tag.get_text(" ", strip=True).upper()
        if sku in tag_text or sku_norm in tag_text.replace("-", "").replace(
            "_", ""
        ):
            qty_input = tag.find(
                "input", attrs={"name": re.compile(r"qty|quantity", re.I)}
            )
            if qty_input:
                try:
                    return int(qty_input.get("value", "1"))
                except (ValueError, TypeError):
                    pass
            match = re.search(
                r"(\d+)\s*(?:unidades?|peças?|itens?)", tag_text, re.I
            )
            if match:
                return int(match.group(1))

    for input_ in soup.find_all("input"):
        name = (input_.get("name") or "").lower()
        if "qty" in name or "quantity" in name:
            try:
                return int(input_.get("value", "1"))
            except (ValueError, TypeError):
                pass

    match = re.search(r"(\d+)\s*(?:unidades?|peças?|itens?)", texto, re.I)
    if match:
        return int(match.group(1))
    return 1


def limpar_carrinho(page: Page) -> None:
    """
    P0: esvazia o carrinho para não acumular IPI entre SKUs.
    Tenta endpoints de clear; fallback remove itens na página /cart.
    """
    csrf = obter_token_csrf(page)
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    if csrf:
        headers["CSRFToken"] = csrf

    for endpoint in (
        "https://www.wago.com/br/cart/empty",
        "https://www.wago.com/br/cart/clear",
        "https://www.wago.com/br/cart/removeAll",
    ):
        try:
            res = page.request.post(endpoint, headers=headers, timeout=10000)
            if res.ok:
                return
        except Exception:
            pass
        try:
            res = page.request.delete(endpoint, headers=headers, timeout=10000)
            if res.ok:
                return
        except Exception:
            pass

    # Fallback: abrir carrinho e clicar em remover/esvaziar
    cart_page = None
    try:
        cart_page = page.context.new_page()
        cart_page.goto(WAGO_CART, timeout=30000)
        cart_page.wait_for_timeout(1500)
        for selector in (
            'button:has-text("Esvaziar")',
            'a:has-text("Esvaziar")',
            'button:has-text("Remover todos")',
            'button:has-text("Remover")',
            'a:has-text("Remover")',
            '[class*="remove"]',
            'button[aria-label*="Remover"]',
            'button[aria-label*="Remove"]',
        ):
            try:
                loc = cart_page.locator(selector)
                count = loc.count()
                for i in range(min(count, 20)):
                    try:
                        loc.nth(i).click(timeout=2000)
                        cart_page.wait_for_timeout(500)
                    except Exception:
                        break
            except Exception:
                continue
    except Exception:
        pass
    finally:
        if cart_page:
            try:
                cart_page.close()
            except Exception:
                pass


def capturar_ipi_carrinho(page: Page, sku: str) -> float:
    """Adiciona o produto ao carrinho, lê IPI unitário e limpa o carrinho."""
    csrf = obter_token_csrf(page)
    headers = {
        "X-Requested-With": "XMLHttpRequest",
        "Accept": "application/json, text/plain, */*",
    }
    if csrf:
        headers["CSRFToken"] = csrf

    adicionado = False
    for endpoint in (
        "https://www.wago.com/br/cart/add",
        "https://www.wago.com/br/cart/add?source=product",
    ):
        for payload in (
            {"productCode": sku, "qty": "1"},
            {"productCode": sku},
        ):
            try:
                res = page.request.post(
                    endpoint, form=payload, headers=headers, timeout=15000
                )
                if res.ok:
                    adicionado = True
                    break
            except Exception:
                pass
        if adicionado:
            break

    if not adicionado:
        return 0.0

    unit_ipi = 0.0
    cart_page = None
    try:
        cart_page = page.context.new_page()
        cart_page.goto(WAGO_CART, timeout=30000)
        cart_page.wait_for_timeout(2500)
        try:
            cart_page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        soup = BeautifulSoup(cart_page.content(), "html.parser")
        texto = soup.get_text(" ", strip=True)
        total_ipi = _parse_valor_ipi(texto)
        if total_ipi is not None:
            total_qtd = _extrair_quantidade_carrinho(soup, sku, texto)
            if total_qtd <= 0:
                total_qtd = 1
            unit_ipi = round(total_ipi / total_qtd, 2)
    except Exception:
        unit_ipi = 0.0
    finally:
        if cart_page:
            try:
                cart_page.close()
            except Exception:
                pass
        # P0: sempre limpar, mesmo se a leitura falhou
        limpar_carrinho(page)

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
