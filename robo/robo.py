"""Robo do Leilao Explore.

Baixa a lista de imoveis da Caixa (por estado), compara com a lista anterior
e grava docs/dados.json com a data em que cada imovel apareceu pela primeira
vez e o preco anterior quando o preco muda.

Uso: python robo/robo.py            -> baixa da Caixa
     python robo/robo.py arquivo.csv -> usa um CSV local (teste)
"""
import csv
import io
import json
import os
import re
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone

ESTADOS = ["PA"]
URL = "https://venda-imoveis.caixa.gov.br/listaweb/Lista_imoveis_{uf}.csv"
SAIDA = os.path.join(os.path.dirname(__file__), "..", "docs", "dados.json")
CAMPOS = ["id", "cidade", "bairro", "endereco", "preco", "avaliacao", "desconto",
          "financiamento", "tipo", "quartos", "area_privativa", "area_terreno",
          "modalidade", "visto_em", "preco_ant"]
DIAS_REMOVIDOS = 30
FUSO = timezone(timedelta(hours=-3))


def sem_acento(t):
    t = unicodedata.normalize("NFD", t or "")
    return "".join(c for c in t if unicodedata.category(c) != "Mn").lower().strip()


def numero(t):
    t = (t or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return 0.0
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        return round(float(t), 2)
    except ValueError:
        return 0.0


def num_desc(regex, texto):
    m = re.search(regex, texto, re.I)
    return float(m.group(1).replace(",", ".")) if m else 0


def baixar(uf):
    req = urllib.request.Request(URL.format(uf=uf), headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
        "Accept": "text/csv,text/plain,*/*",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Referer": "https://venda-imoveis.caixa.gov.br/sistema/download-lista.asp",
    })
    with urllib.request.urlopen(req, timeout=90) as r:
        return r.read()


def decodificar(dados):
    if dados.startswith(b"\xef\xbb\xbf"):
        return dados[3:].decode("utf-8", "replace")
    try:
        return dados.decode("utf-8")
    except UnicodeDecodeError:
        return dados.decode("latin-1")


def ler_csv(texto):
    linhas = texto.splitlines()
    inicio = None
    for i, l in enumerate(linhas):
        n = sem_acento(l)
        if "uf" in n.split(";") or (";uf;" in n and "cidade" in n):
            inicio = i
            break
    if inicio is None:
        raise RuntimeError("Cabecalho da lista nao encontrado (a Caixa pode ter bloqueado o acesso).")
    leitor = csv.reader(io.StringIO("\n".join(linhas[inicio:])), delimiter=";")
    cab = [sem_acento(c) for c in next(leitor)]

    def col(*chaves):
        for i, c in enumerate(cab):
            if all(k in c for k in chaves):
                return i
        return None

    ix = {
        "id": 0, "cidade": col("cidade"), "bairro": col("bairro"),
        "endereco": col("endereco"), "preco": col("preco"),
        "avaliacao": col("avaliacao"), "desconto": col("desconto"),
        "financiamento": col("financiamento"), "descricao": col("descricao"),
        "modalidade": col("modalidade"),
    }
    imoveis = []
    for row in leitor:
        if len(row) < 6 or not row[0].strip():
            continue
        g = lambda k: row[ix[k]].strip() if ix[k] is not None and ix[k] < len(row) else ""
        idi = re.sub(r"\D", "", g("id"))
        if not idi:
            continue
        desc = g("descricao")
        tipo = desc.split(",")[0].strip() or "Imóvel"
        fin = 1 if sem_acento(g("financiamento")).startswith("s") else 0
        imoveis.append({
            "id": idi, "cidade": g("cidade"), "bairro": g("bairro"),
            "endereco": g("endereco"), "preco": numero(g("preco")),
            "avaliacao": numero(g("avaliacao")),
            "desconto": numero(g("desconto").replace("%", "")),
            "financiamento": fin, "tipo": tipo,
            "quartos": int(num_desc(r"(\d+)\s*qto", desc)),
            "area_privativa": num_desc(r"([\d.,]+)\s*de\s*[áa]rea\s*privativa", desc),
            "area_terreno": num_desc(r"([\d.,]+)\s*de\s*[áa]rea\s*do\s*terreno", desc),
            "modalidade": g("modalidade"),
        })
    if len(imoveis) < 5:
        raise RuntimeError(f"Lista veio quase vazia ({len(imoveis)} imoveis); nada foi alterado.")
    return imoveis


def carregar_anterior():
    if not os.path.exists(SAIDA):
        return {}, {}, []
    with open(SAIDA, encoding="utf-8") as f:
        d = json.load(f)
    campos = d.get("campos", CAMPOS)
    ant = {}
    for linha in d.get("imoveis", []):
        r = dict(zip(campos, linha))
        ant[r["id"]] = r
    return ant, d, d.get("removidos", [])


def main():
    hoje = datetime.now(FUSO).strftime("%Y-%m-%d")
    novos_lista = []
    for uf in ESTADOS:
        if len(sys.argv) > 1:
            with open(sys.argv[1], "rb") as f:
                texto = decodificar(f.read())
        else:
            texto = decodificar(baixar(uf))
        novos_lista += ler_csv(texto)

    ant, dados_ant, removidos = carregar_anterior()
    seed_data = dados_ant.get("atualizado_iso") or hoje
    atual_ids = set()
    resultado = []
    n_novos = n_preco = 0
    for im in novos_lista:
        atual_ids.add(im["id"])
        velho = ant.get(im["id"])
        if velho:
            im["visto_em"] = velho.get("visto_em") or seed_data
            im["preco_ant"] = velho.get("preco_ant") or 0
            if velho.get("preco") and abs(velho["preco"] - im["preco"]) >= 1:
                im["preco_ant"] = velho["preco"]
                n_preco += 1
        else:
            im["visto_em"] = hoje
            im["preco_ant"] = 0
            n_novos += 1
        resultado.append([im[c] for c in CAMPOS])

    limite = (datetime.now(FUSO) - timedelta(days=DIAS_REMOVIDOS)).strftime("%Y-%m-%d")
    removidos = [r for r in removidos if r.get("saiu_em", "") >= limite and r["id"] not in atual_ids]
    ja = {r["id"] for r in removidos}
    n_saiu = 0
    for idi, r in ant.items():
        if idi not in atual_ids and idi not in ja:
            removidos.append({"id": idi, "cidade": r.get("cidade"), "bairro": r.get("bairro"),
                              "tipo": r.get("tipo"), "preco": r.get("preco"), "saiu_em": hoje})
            n_saiu += 1

    resultado.sort(key=lambda x: (x[1], -x[6]))
    saida = {
        "atualizado": datetime.now(FUSO).strftime("%d/%m/%Y %H:%M"),
        "atualizado_iso": hoje,
        "fonte": "Caixa", "estados": ESTADOS, "campos": CAMPOS,
        "resumo": {"total": len(resultado), "novos": n_novos, "saiu": n_saiu, "preco_mudou": n_preco},
        "imoveis": resultado, "removidos": removidos,
    }
    with open(SAIDA, "w", encoding="utf-8") as f:
        json.dump(saida, f, ensure_ascii=False, separators=(",", ":"))
    print(f"Total {len(resultado)} | novos {n_novos} | sairam {n_saiu} | preco mudou {n_preco}")


if __name__ == "__main__":
    main()
