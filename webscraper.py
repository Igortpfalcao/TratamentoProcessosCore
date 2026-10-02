#!/usr/bin/env python3
"""
Scraper do TRF1 - Consulta Processual (JFBA)

Fluxo:
  1. Abre a listagem de processos da parte.
  2. Para cada linha, pega o link do processo (processo.php?proc=...).
  3. Dentro de cada processo, coleta o conteúdo das divs
     #aba_processos  (dados gerais, em pares chave/valor)
     #aba_movimentacoes (tabela de movimentações)
  4. Salva tudo em CSV e JSON.

Uso:
  pip install requests beautifulsoup4 lxml
  python scraper_trf1.py
  python scraper_trf1.py --limite 5          # teste com 5 processos
  python scraper_trf1.py --delay 1.5         # pausa entre requisições

Reexecutar é seguro: o HTML de cada processo fica em cache (pasta cache_html/),
então se o script cair no meio ele continua de onde parou.
"""

import argparse
import csv
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

URL_LISTA = (
    "https://processual.trf1.jus.br/consultaProcessual/parte/listarProcessos.php"
    "?id=803489&tipo=N"
    "&nome=CONSELHO+REGIONAL+DE+REPRESENTANTES+COMERCIAIS+DA+BAHIA+CORE+BA"
    "&mostrarBaixados=S&secao=BA&cnpj=15176951000193"
)

# Ids informados por você. Deixei alternativas (hífen / singular) como fallback,
# porque o menu da página usa âncoras como #aba-processo e #aba-movimentacao.
IDS_ABA_PROCESSO = ["aba_processos", "aba_processo", "aba-processos", "aba-processo"]
IDS_ABA_MOVIMENTACAO = ["aba_movimentacoes", "aba_movimentacao", "aba-movimentacoes", "aba-movimentacao"]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
}

CACHE_DIR = Path("cache_html")
SAIDA_DIR = Path("saida_coreba")  # sobrescrito por --saida


# --------------------------------------------------------------------------
# Utilidades
# --------------------------------------------------------------------------
def limpar(texto: str) -> str:
    """Normaliza espaços/quebras de linha."""
    return re.sub(r"\s+", " ", texto or "").strip()


def baixar(sessao: requests.Session, url: str, tentativas: int = 4) -> str:
    """GET com retry e backoff."""
    ultimo_erro = None
    for i in range(1, tentativas + 1):
        try:
            r = sessao.get(url, headers=HEADERS, timeout=30)
            r.raise_for_status()
            if not r.encoding or r.encoding.lower() == "iso-8859-1":
                r.encoding = r.apparent_encoding or "utf-8"
            return r.text
        except requests.RequestException as e:
            ultimo_erro = e
            espera = 2 ** i
            print(f"    ! erro ({e}); tentativa {i}/{tentativas}, aguardando {espera}s", file=sys.stderr)
            time.sleep(espera)
    raise RuntimeError(f"Falha ao baixar {url}: {ultimo_erro}")


def achar_div(soup: BeautifulSoup, ids: list[str]):
    for _id in ids:
        el = soup.find(id=_id)
        if el:
            return el
    return None


# XPaths das tabelas (sem /tbody, pois o navegador o insere automaticamente)
XPATH_PROCESSO = "/html/body/div[1]/div[4]/div[2]/div[3]/div[1]/div[1]/table"
XPATH_MOVIMENTACAO = "/html/body/div[1]/div[4]/div[2]/div[3]/div[1]/div[2]/table"


def achar_por_xpath(html: str, xpath: str):
    """Plano B: localiza a tabela pelo XPath e devolve como objeto BeautifulSoup."""
    from lxml import etree, html as lxml_html

    nos = lxml_html.fromstring(html).getroottree().xpath(xpath)
    if not nos:
        return None
    return BeautifulSoup(etree.tostring(nos[0], encoding="unicode"), "lxml")


# --------------------------------------------------------------------------
# 1) Listagem
# --------------------------------------------------------------------------
def coletar_lista(sessao: requests.Session, url_lista: str = URL_LISTA) -> list[dict]:
    html = baixar(sessao, url_lista)
    soup = BeautifulSoup(html, "lxml")

    processos = []
    vistos = set()
    for a in soup.select('a[href*="processo.php?proc="]'):
        href = urljoin(url_lista, a["href"])
        if href in vistos:
            continue
        vistos.add(href)

        tr = a.find_parent("tr")
        celulas = [limpar(td.get_text()) for td in tr.find_all("td")] if tr else []
        processos.append(
            {
                "numero_novo": limpar(a.get_text()),
                "numero_antigo": celulas[1] if len(celulas) > 1 else "",
                "classe_cod": celulas[2] if len(celulas) > 2 else "",
                "classe_desc": celulas[3] if len(celulas) > 3 else "",
                "url": href,
            }
        )
    return processos


# --------------------------------------------------------------------------
# 2) Página do processo
# --------------------------------------------------------------------------
def parse_aba_processo(div) -> dict:
    """Extrai pares chave/valor (ex.: 'Vara:' -> '20ª VARA SALVADOR')."""
    dados = {}
    if div is None:
        return dados
    for tr in div.find_all("tr"):
        cels = tr.find_all(["th", "td"])
        if len(cels) == 2:
            chave = limpar(cels[0].get_text()).rstrip(":").strip()
            valor = limpar(cels[1].get_text())
            if chave:
                dados[chave] = valor
    return dados


def parse_aba_movimentacao(div) -> list[dict]:
    """Extrai a tabela de movimentações (Data, Cod, Descrição, Complemento)."""
    # O tbody do site não tem linha de cabeçalho: as colunas são sempre estas.
    colunas = ["Data", "Cod", "Descrição", "Complemento"]
    movs = []
    if div is None:
        return movs
    for tr in div.find_all("tr"):
        tds = [limpar(td.get_text()) for td in tr.find_all("td")]  # th (cabeçalho) é ignorado
        if len(tds) < 3:
            continue
        tds += [""] * (len(colunas) - len(tds))
        movs.append(dict(zip(colunas, tds[: len(colunas)])))
    return movs


def coletar_processo(sessao: requests.Session, item: dict, delay: float) -> dict:
    # nome do arquivo de cache = valor do parâmetro proc
    m = re.search(r"proc=(\d+)", item["url"])
    chave = m.group(1) if m else re.sub(r"\W+", "_", item["numero_novo"])
    arq = CACHE_DIR / f"{chave}.html"

    if arq.exists():
        html = arq.read_text(encoding="utf-8")
    else:
        html = baixar(sessao, item["url"])
        arq.write_text(html, encoding="utf-8")
        time.sleep(delay)  # só espera quando realmente bateu no servidor

    soup = BeautifulSoup(html, "lxml")
    div_proc = achar_div(soup, IDS_ABA_PROCESSO) or achar_por_xpath(html, XPATH_PROCESSO)
    div_mov = achar_div(soup, IDS_ABA_MOVIMENTACAO) or achar_por_xpath(html, XPATH_MOVIMENTACAO)

    if div_proc is None or div_mov is None:
        print(
            f"    ! {item['numero_novo']}: div não encontrada "
            f"(processo={'ok' if div_proc else 'NÃO'}, movimentacoes={'ok' if div_mov else 'NÃO'})",
            file=sys.stderr,
        )

    return {
        **item,
        "dados": parse_aba_processo(div_proc),
        "movimentacoes": parse_aba_movimentacao(div_mov),
    }


# --------------------------------------------------------------------------
# 3) Saída
# --------------------------------------------------------------------------
def atribuir_ids(resultados: list[dict], id_inicial: int, mov_id_inicial: int) -> None:
    """
    processo_id: sequencial a partir de id_inicial, na ordem da listagem.
    movimentacao_id: sequencial global a partir de mov_id_inicial; dentro de cada
    processo a movimentação mais ANTIGA recebe o menor ID (o site lista da mais nova
    para a mais antiga, então percorremos de trás para frente).
    O CSV continua na ordem do site.
    """
    proximo_mov = mov_id_inicial
    for i, r in enumerate(resultados):
        pid = id_inicial + i
        r["processo_id"] = pid
        ids = {}
        for idx in reversed(range(len(r["movimentacoes"]))):
            ids[idx] = proximo_mov
            proximo_mov += 1
        r["movimentacoes"] = [
            {"movimentacao_id": ids[idx], "processo_id": pid, **mv}
            for idx, mv in enumerate(r["movimentacoes"])
        ]


def salvar(resultados: list[dict]) -> None:
    SAIDA_DIR.mkdir(exist_ok=True)

    # JSON completo
    (SAIDA_DIR / "processos_completo.json").write_text(
        json.dumps(resultados, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    # CSV 1: uma linha por processo (união de todas as chaves da aba_processos)
    campos_dados = []
    for r in resultados:
        for k in r["dados"]:
            if k not in campos_dados:
                campos_dados.append(k)
    base = ["processo_id", "numero_novo", "numero_antigo", "classe_cod", "classe_desc", "url"]
    with open(SAIDA_DIR / "processos.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=base + campos_dados + ["qtd_movimentacoes"], delimiter=";")
        w.writeheader()
        for r in resultados:
            linha = {k: r[k] for k in base}
            linha.update(r["dados"])
            linha["qtd_movimentacoes"] = len(r["movimentacoes"])
            w.writerow(linha)

    # CSV 2: uma linha por movimentação (formato "longo")
    campos_mov = []
    for r in resultados:
        for mv in r["movimentacoes"]:
            for k in mv:
                if k not in campos_mov:
                    campos_mov.append(k)
    with open(SAIDA_DIR / "movimentacoes.csv", "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=["numero_novo"] + campos_mov, delimiter=";")
        w.writeheader()
        for r in resultados:
            for mv in r["movimentacoes"]:
                w.writerow({"numero_novo": r["numero_novo"], **mv})

    print(f"\nPronto! Arquivos em ./{SAIDA_DIR}/")
    print("  - processos.csv, movimentacoes.csv, processos_completo.json")


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limite", type=int, default=None, help="processar só N processos (teste)")
    ap.add_argument("--delay", type=float, default=1.0, help="segundos entre requisições")
    ap.add_argument("--url", default=URL_LISTA, help="URL da listagem de processos")
    ap.add_argument("--saida", default=None, help="pasta de saída (padrão: saida_coreba)")
    ap.add_argument("--id-inicial", type=int, default=4949,
                    help="primeiro processo_id (último usado no banco + 1)")
    ap.add_argument("--mov-id-inicial", type=int, default=259608,
                    help="primeiro movimentacao_id (último usado na tabela de movimentos + 1)")
    args = ap.parse_args()

    global SAIDA_DIR
    if args.saida:
        SAIDA_DIR = Path(args.saida)
    CACHE_DIR.mkdir(exist_ok=True)
    sessao = requests.Session()

    print("Buscando listagem de processos...")
    lista = coletar_lista(sessao, args.url)
    print(f"{len(lista)} processos encontrados.")
    if args.limite:
        lista = lista[: args.limite]

    resultados = []
    for i, item in enumerate(lista, 1):
        print(f"[{i}/{len(lista)}] {item['numero_novo']}")
        try:
            resultados.append(coletar_processo(sessao, item, args.delay))
        except Exception as e:
            print(f"    ! pulando {item['numero_novo']}: {e}", file=sys.stderr)

    atribuir_ids(resultados, args.id_inicial, args.mov_id_inicial)
    salvar(resultados)


if __name__ == "__main__":
    main()