import os
import re
import json
import time
import logging
import requests
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("datajud")

# ---------- config ----------
TAMANHO_LOTE = 20
TENTATIVAS = 4
PAUSA_ENTRE_REQ = 1.0
REVERIFICAR_APOS_DIAS = 7

TRIBUNAIS = [
    # nome, url, (J, TR) do número CNJ (posições 13 e 14:16)
    {"nome": "TJBA", "url": "https://api-publica.datajud.cnj.jus.br/api_publica_tjba/_search",
     "j": "8", "tr": "05"},
    {"nome": "TRF1", "url": "https://api-publica.datajud.cnj.jus.br/api_publica_trf1/_search",
     "j": "4", "tr": "01"},
]

senhaDB = os.environ.get("DbSenha")
apikey = "cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw=="
if not senhaDB or not apikey:
    raise ValueError("Defina as variáveis de ambiente DbSenha e DatajudKey")

engine = create_engine(
    f"postgresql+psycopg2://postgres:{senhaDB}@localhost:5432/ExeJud",
    pool_pre_ping=True,
)

sessao = requests.Session()
sessao.headers.update({"Authorization": f"APIKey {apikey}", "Content-Type": "application/json"})


def so_digitos(n: str) -> str:
    return re.sub(r"\D", "", n or "")


def formatar_cnj(n: str) -> str:
    n = so_digitos(n)
    if len(n) != 20:
        raise ValueError(f"Número CNJ inválido: {n}")
    return f"{n[:7]}-{n[7:9]}.{n[9:13]}.{n[13]}.{n[14:16]}.{n[16:]}"


# ---------- API ----------
def _requisitar(url, numeros):
    corpo_req = {"size": len(numeros) * 2, "query": {"terms": {"numeroProcesso": numeros}}}
    ultimo_erro = None
    for i in range(TENTATIVAS):
        try:
            r = sessao.post(url, json=corpo_req, timeout=120)
            if r.status_code == 429 or r.status_code >= 500:
                espera = int(r.headers.get("Retry-After", 0)) or 2 ** i * 5
                raise RuntimeError(f"HTTP {r.status_code} (aguardando {espera}s)")
            r.raise_for_status()
            corpo = r.json()
            if corpo.get("timed_out") or corpo["_shards"]["failed"] > 0:
                raise RuntimeError("resposta incompleta (timed_out/shards com falha)")
            return corpo["hits"]["hits"]
        except (requests.RequestException, ValueError, KeyError, RuntimeError) as e:
            ultimo_erro = e
            m = re.search(r"aguardando (\d+)s", str(e))
            espera = int(m.group(1)) if m else 2 ** i * 5
            log.warning("Tentativa %d/%d falhou: %s", i + 1, TENTATIVAS, e)
            time.sleep(espera)
    raise RuntimeError(f"lote falhou após {TENTATIVAS} tentativas: {ultimo_erro}")


def consultar_lote(url, numeros):
    """Retorna {numero: _source}. Números não encontrados ficam ausentes do dict."""
    try:
        hits = _requisitar(url, numeros)
    except RuntimeError:
        if len(numeros) == 1:
            raise
        meio = len(numeros) // 2
        log.info("Dividindo lote de %d em %d + %d", len(numeros), meio, len(numeros) - meio)
        resultado = {}
        for parte in (numeros[:meio], numeros[meio:]):
            try:
                resultado.update(consultar_lote(url, parte))
            except RuntimeError as e:
                log.error("Falha definitiva neste ciclo para %s: %s", parte, e)
                for n in parte:
                    resultado[n] = "FALHA"
        return resultado

    achados = {}
    for h in hits:
        src = h["_source"]
        n = src["numeroProcesso"]
        if n not in achados or (src.get("grau") == "G1" and achados[n].get("grau") != "G1"):
            achados[n] = src
    time.sleep(PAUSA_ENTRE_REQ)
    return achados


# ---------- banco ----------
def marcar_nao_encontrado(processo_id):
    with engine.begin() as conn:
        conn.execute(text("""
            UPDATE processos SET datajud_verificado_em = now(), datajud_encontrado = false
             WHERE id = :id"""), {"id": processo_id})


def gravar(processo_id, src):
    ult = src.get("dataHoraUltimaAtualizacao")
    movimentos = src.get("movimentos") or []

    with engine.begin() as conn:
        mudou = True
        if ult:
            mudou = conn.execute(text("""
                SELECT datajud_atualizacao IS DISTINCT FROM CAST(:u AS timestamptz)
                  FROM processos WHERE id = :id"""), {"u": ult, "id": processo_id}).scalar()

        if mudou:
            conn.execute(text("DELETE FROM movimentos WHERE processo_id = :id"), {"id": processo_id})
            if movimentos:
                df = pd.DataFrame(
                    {
                        "processo_id": processo_id,
                        "data_hora": m.get("dataHora"),
                        "nome": m.get("nome"),
                        "dados_json": m,
                    }
                    for m in movimentos
                )
                df["data_hora"] = pd.to_datetime(df["data_hora"], utc=True)
                df.to_sql("movimentos", conn, if_exists="append", index=False,
                          dtype={"dados_json": JSONB})

        conn.execute(text("""
            UPDATE processos
               SET datajud_verificado_em = now(),
                   datajud_encontrado = true,
                   datajud_atualizacao = COALESCE(CAST(:u AS timestamptz), datajud_atualizacao)
             WHERE id = :id"""), {"u": ult, "id": processo_id})
    return mudou, len(movimentos)


def buscar_pendentes(j, tr):
    with engine.connect() as conn:
        return conn.execute(text("""
            SELECT id, numero_processo FROM processos
             WHERE (datajud_verificado_em IS NULL
                    OR datajud_verificado_em < now() - make_interval(days => :dias))
               AND regexp_replace(numero_processo, '\\D', '', 'g') ~ ('^.{13}' || :j || :tr)
             ORDER BY datajud_verificado_em NULLS FIRST, id"""),
            {"dias": REVERIFICAR_APOS_DIAS, "j": j, "tr": tr}).fetchall()


# ---------- por tribunal ----------
def processar_tribunal(nome, url, j, tr):
    pendentes = buscar_pendentes(j, tr)
    log.info("[%s] %d processos pendentes", nome, len(pendentes))
    if not pendentes:
        return

    mapa = {}
    for pid, numero in pendentes:
        d = so_digitos(numero)
        if len(d) != 20:
            log.warning("[%s] número inválido ignorado (id=%s): %s", nome, pid, numero)
            continue
        mapa[d] = pid

    numeros = list(mapa)
    for i in range(0, len(numeros), TAMANHO_LOTE):
        lote = numeros[i:i + TAMANHO_LOTE]
        try:
            achados = consultar_lote(url, lote)
        except RuntimeError as e:
            log.error("[%s] lote pulado: %s", nome, e)
            continue

        for n in lote:
            pid = mapa[n]
            src = achados.get(n)
            try:
                if src == "FALHA":
                    continue  # permanece pendente
                if src is None:
                    marcar_nao_encontrado(pid)
                    log.info("[%s] id=%s %s: não encontrado", nome, pid, n)
                else:
                    mudou, qtd = gravar(pid, src)
                    log.info("[%s] id=%s %s: %s (%d movimentos)", nome, pid, n,
                              "atualizado" if mudou else "sem mudanças", qtd)
            except Exception:
                log.exception("[%s] erro ao gravar id=%s %s", nome, pid, n)
        log.info("[%s] progresso: %d/%d", nome, min(i + TAMANHO_LOTE, len(numeros)), len(numeros))


if __name__ == "__main__":
    for t in TRIBUNAIS:
        processar_tribunal(t["nome"], t["url"], t["j"], t["tr"])
    log.info("Ciclo concluído.")