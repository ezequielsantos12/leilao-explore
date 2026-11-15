"""Robo dos sites de leiloeiros do Leilao Explore.

Abre cada pagina de busca num navegador de verdade (Chromium/Playwright),
espera a lista carregar, le os cards de imoveis (link + texto com R$) e grava
docs/sites.json marcando a data em que cada imovel apareceu pela primeira vez.
"""
import asyncio
import json
import os
import re
import sys
import unicodedata
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import async_playwright

AQUI = os.path.dirname(__file__)
SAIDA = os.environ.get("SITES_SAIDA", os.path.join(AQUI, "..", "docs", "sites.json"))
CAIXA = os.path.join(AQUI, "..", "docs", "dados.json")
FUSO = timezone(timedelta(hours=-3))

with open(os.environ.get("SITES_JSON", os.path.join(AQUI, "sites.json")), encoding="utf-8") as f:
    SITES = json.load(f)

# Le os cards: cada link cujo "bloco" ao redor tem um valor em R$.
JS_CARDS = r"""
() => {
  const vistos = new Map();
  const ok = h => h && !h.startsWith('javascript') && !h.startsWith('mailto') && !h.startsWith('tel') && !h.endsWith('#');
  for (const a of document.querySelectorAll('a[href]')) {
    const href = a.href;
    if (!ok(href) || href.split('#')[0] === location.href.split('#')[0]) continue;
    let el = a, txt = '';
    for (let i = 0; i < 7 && el; i++, el = el.parentElement) {
      const t = (el.innerText || '').trim();
      if (t.length > 1800) break;
      if (/R\$\s?\d/.test(t)) {
        // bloco de um card so: poucos links diferentes e poucos precos
        const links = new Set([...el.querySelectorAll('a[href]')].map(x => x.href.split('#')[0]));
        const precos = (t.match(/R\$\s?\d/g) || []).length;
        if (links.size <= 4 && precos <= 4) txt = t;
        break;
      }
    }
    if (!txt) continue;
    const titulo = (a.getAttribute('title') || a.innerText || '').trim();
    const atual = vistos.get(href);
    if (!atual || txt.length > atual.txt.length) vistos.set(href, {href, txt, titulo});
  }
  return [...vistos.values()];
}
"""


# Leitores especiais para sites que o leitor generico nao entende.
JS_LEITORES = {
  # Superbid: os dados ja vem prontos dentro da pagina (__NEXT_DATA__)
  "superbid": r"""
() => {
  const el = document.getElementById('__NEXT_DATA__');
  if (!el) return [];
  const d = JSON.parse(el.textContent).props.pageProps.offersList || {};
  const links = [...document.querySelectorAll('a[href*="/oferta/"]')].map(a => a.href);
  return (d.offers || []).map(o => {
    const p = o.product || {};
    const href = links.find(h => h.endsWith('-' + o.id)) || (location.origin + '/oferta/' + o.id);
    const txt = [p.shortDesc, 'Encerra: ' + (o.endDate || ''), 'Lance: ' + (o.priceFormatted || ''),
                 (o.auction && o.auction.desc) || ''].join('\n');
    return {href, titulo: p.shortDesc || '', txt, preco: o.priceFormatted || ''};
  });
}
""",
  # Norte Leiloes: cards sem preco na lista; o titulo vem como "CIDADE/UF - TIPO - DESCRICAO"
  "norte": r"""
() => {
  const m = new Map();
  for (const a of document.querySelectorAll('a[href*="/lote/"]')) {
    let el = a;
    for (let i = 0; i < 8 && el; i++, el = el.parentElement) {
      const t = el.innerText || '';
      if (/ID \d+/.test(t) && /Lote \d+/.test(t)) break;
    }
    if (!el) continue;
    const t = el.innerText.trim();
    const linha = t.split('\n').find(l => / - /.test(l) && /^\d{9,}/.test(l.trim())) || '';
    const titulo = linha.replace(/^\s*\d+\s*-\s*/, '');
    if (!m.has(a.href) || t.length < m.get(a.href).txt.length)
      m.set(a.href, {href: a.href, titulo, txt: t.slice(0, 600), preco: ''});
  }
  return [...m.values()];
}
""",
  # Lance no Leilao: os cards nao tem link <a>, abrem por goTo('lote.php?...')
  "lancenoleilao": r"""
() => {
  const out = [];
  for (const c of document.querySelectorAll('div.lotePadrao')) {
    const oc = [...c.querySelectorAll('[onclick]')].map(e => e.getAttribute('onclick')).find(s => /goTo\('lote/.test(s));
    if (!oc) continue;
    const rel = oc.match(/goTo\('([^']+)'/)[1];
    const txt = (c.innerText || '').replace(/\n{2,}/g, '\n').trim();
    const cid = (txt.match(/([A-ZÀ-Ú][A-ZÀ-Ú .'-]+\([A-Z]{2}\))/) || [''])[0].trim();
    let desc = txt.split(/\([A-Z]{2}\)/)[1] || '';
    desc = desc.replace(/^\s*Descri[çc][ãa]o( legal)?:\s*/i, '').split(/\.(?=\s|[A-Za-z])/)[0];
    const ini = txt.match(/Inicial:\s*(R\$\s?[\d.]+,\d{2})/);
    const ult = txt.match(/[ÚU]ltimo lance:\s*(R\$\s?[\d.]+,\d{2})/);
    let preco = ini ? ini[1] : (ult && !/R\$\s?0,00/.test(ult[1]) ? ult[1] : '');
    out.push({href: new URL(rel, location.href).href, titulo: ((cid ? cid + ' — ' : '') + desc).slice(0, 160),
              txt: txt.slice(0, 1500), preco});
  }
  return out;
}
""",
}

UFS_NORTE = {"PA", "AM", "AP", "RR", "RO", "AC", "TO"}


def fora_do_norte(texto):
    """True se o texto cita uma UF (ex.: BREJO DE AREIA/MA) e nenhuma e do Norte."""
    ufs = set(re.findall(r"/([A-Z]{2})\b", texto or ""))
    return bool(ufs) and not (ufs & UFS_NORTE)


def sem_acento(t):
    t = unicodedata.normalize("NFD", t or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").upper()


def limpar_link(h):
    u = urlparse(h)
    q = "&".join(p for p in u.query.split("&") if p and not p.lower().startswith("utm_"))
    return urlunparse((u.scheme, u.netloc.lower(), u.path.rstrip("/"), "", q, ""))


def cidades_pa():
    try:
        with open(CAIXA, encoding="utf-8") as f:
            d = json.load(f)
        i = d["campos"].index("cidade")
        nomes = {sem_acento(l[i]) for l in d["imoveis"]}
    except Exception:
        nomes = set()
    nomes |= {"BELEM", "ANANINDEUA", "MARITUBA", "BENEVIDES", "CASTANHAL", "SANTA BARBARA DO PARA",
              "SANTA IZABEL DO PARA", "SANTA ISABEL DO PARA", "MARABA", "PARAUAPEBAS", "SANTAREM",
              "ALTAMIRA", "REDENCAO", "TUCURUI", "BARCARENA", "ABAETETUBA", "PARAGOMINAS", "SALINOPOLIS"}
    return sorted(nomes, key=len, reverse=True)


CIDADES = cidades_pa()


def montar_item(site, c):
    txt = re.sub(r"\s+\n", "\n", c["txt"])
    linhas = [l.strip() for l in txt.split("\n") if l.strip()]
    precos = [p for p in re.findall(r"R\$\s?[\d.]+(?:,\d{2})?", txt) if not re.fullmatch(r"R\$\s?0(,00)?", p)]
    if c.get("preco"):
        precos = [c["preco"]] + [p for p in precos if p != c["preco"]]
    titulo = c["titulo"] if len(c.get("titulo") or "") > 12 else ""
    if not titulo:
        cand = [l for l in linhas if 15 <= len(l) <= 160 and "R$" not in l]
        titulo = max(cand[:6], key=len) if cand else linhas[0][:160]
    norm = sem_acento(titulo + " " + txt + " " + c["href"].replace("-", " "))
    cidade = next((n for n in CIDADES if re.search(r"\b" + re.escape(n) + r"\b", norm)), "")
    if not cidade:  # padrao "BANNACH/PA" ou "BAIAO (PA)"
        m = re.search(r"([A-Z][A-Z ']{2,40}?)\s*(?:/|\()\s*(?:PA|AM|AP|RR|RO|AC|TO)\b", sem_acento(titulo + " " + txt))
        if m:
            cidade = m.group(1).strip()
    return {
        "id": limpar_link(c["href"]), "site": site["nome"], "titulo": titulo[:160],
        "preco": precos[0] if precos else "", "precos": precos[:3], "cidade": cidade,
        "texto": " · ".join(linhas[:8])[:400],
    }


async def abrir(pagina, url, site):
    await pagina.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        await pagina.wait_for_load_state("networkidle", timeout=20000)
    except Exception:
        pass
    await pagina.wait_for_timeout(site.get("espera_ms", 4000))
    for _ in range(site.get("rolagens", 4)):  # rola para carregar a lista toda
        await pagina.mouse.wheel(0, 2500)
        await pagina.wait_for_timeout(1200)
    js = JS_LEITORES.get(site.get("leitor"), JS_CARDS)
    return await pagina.evaluate(js)


async def ler_site(navegador, site):
    pagina = await navegador.new_page()
    try:
        if site.get("aquecer"):
            # abre a pagina inicial antes, para passar pela verificacao da Cloudflare
            await pagina.goto(site["aquecer"], wait_until="domcontentloaded", timeout=60000)
            await pagina.wait_for_timeout(8000)
        itens = {}
        paginas = site.get("max_paginas", 1) if "{p}" in site["url"] else 1
        for n in range(1, paginas + 1):
            url = site["url"].replace("{p}", str(n))
            cards = await abrir(pagina, url, site)
            filtro = site.get("link_contem")
            if filtro:
                cards = [c for c in cards if any(f in c["href"] for f in filtro)]
            antes = len(itens)
            for c in cards:
                it = montar_item(site, c)
                if site.get("so_norte") and fora_do_norte(it["titulo"] + " " + c["txt"]):
                    continue
                itens.setdefault(it["id"], it)
            if not cards or len(itens) == antes:
                break  # acabou a lista de paginas
        return list(itens.values()), ""
    except Exception as e:
        return [], str(e)[:200]
    finally:
        await pagina.close()


async def principal():
    hoje = datetime.now(FUSO).strftime("%Y-%m-%d")
    anterior = {"itens": [], "sites": []}
    if os.path.exists(SAIDA):
        with open(SAIDA, encoding="utf-8") as f:
            anterior = json.load(f)
    ant_itens = {i["id"]: i for i in anterior.get("itens", [])}
    ant_ok = {s["nome"] for s in anterior.get("sites", []) if s.get("qtd", 0) > 0}

    async with async_playwright() as p:
        nav = await p.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
        ctx_ua = {"user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}
        navegador = await nav.new_context(locale="pt-BR", viewport={"width": 1366, "height": 900}, **ctx_ua)
        resultados = []
        for site in SITES:
            if site.get("so_atalho"):
                resultados.append((site, [], "atalho"))
                continue
            itens, erro = await ler_site(navegador, site)
            resultados.append((site, itens, erro))
            print(f"{site['nome']}: {len(itens)} itens {erro}")
        await nav.close()

    saida_sites, saida_itens = [], []
    for site, itens, erro in resultados:
        if not itens and erro != "atalho":
            # falhou hoje: mantem o que tinha para nao perder a lista
            velhos = [i for i in ant_itens.values() if i["site"] == site["nome"]]
            saida_itens += velhos
            saida_sites.append({"nome": site["nome"], "url": site["url"], "qtd": len(velhos),
                                "status": "erro", "erro": erro or "nenhum imóvel encontrado"})
            continue
        primeira_vez = site["nome"] not in ant_ok  # 1ª leitura: nada conta como novo
        novos = 0
        for it in itens:
            velho = ant_itens.get(it["id"])
            if velho:
                it["visto_em"] = velho.get("visto_em", "")
                if velho.get("preco") and it["preco"] and velho["preco"] != it["preco"]:
                    it["preco_ant"] = velho["preco"]
            else:
                it["visto_em"] = "" if primeira_vez else hoje
                novos += 0 if primeira_vez else 1
        saida_itens += itens
        saida_sites.append({"nome": site["nome"], "url": site["url"], "qtd": len(itens),
                            "novos": novos, "status": "atalho" if erro == "atalho" else "ok"})

    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump({"atualizado": datetime.now(FUSO).strftime("%d/%m/%Y %H:%M"), "atualizado_iso": hoje,
                   "sites": saida_sites, "itens": saida_itens}, f, ensure_ascii=False, separators=(",", ":"))


if __name__ == "__main__":
    asyncio.run(principal())
