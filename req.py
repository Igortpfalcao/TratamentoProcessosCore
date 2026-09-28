import requests
import json
import pandas as pd
from sqlalchemy import create_engine, text
from sqlalchemy.dialects.postgresql import JSONB
import os

senhaDB = os.environ.get('DbSenha')
if not senhaDB:
    raise ValueError(
       "Variável não encontrada no ambiente"
   )

engine = create_engine(
    "postgresql+psycopg2://postgres:amokeale21@localhost:5432/ExeJud"
)
  
def formatar_cnj(n: str) -> str:
    n = "".join(c for c in n if c.isdigit())
    if len(n) != 20:
        raise ValueError(f"Número CNJ inválido: {n}")
    return f"{n[:7]}-{n[7:9]}.{n[9:13]}.{n[13]}.{n[14:16]}.{n[16:]}"


url = "https://api-publica.datajud.cnj.jus.br/api_publica_tjba/_search"

headers = {
    "Authorization": "APIKey cDZHYzlZa0JadVREZDJCendQbXY6SkJlTzNjLV9TRENyQk1RdnFKZGRQdw==",
    "Content-Type": "application/json"
}

dados = {
    "query": {
        "term": {
            "numeroProcesso": "00000011119928050273"
        }
    }
}

response = requests.post(
    url,
    headers=headers,
    json=dados,
    timeout=80
)
response.raise_for_status()
hits = response.json()["hits"]["hits"]
if not hits:
    raise ValueError("Processo não encontrado na API")

processo = hits[0]["_source"]
numero = processo["numeroProcesso"]
movimentos = processo["movimentos"]

numero_formatado = formatar_cnj(numero)
with engine.begin() as conn:
    row = conn.execute(
        text("SELECT id FROM processos WHERE numero_processo = :numero"),
        {"numero": numero_formatado},
    ).fetchone()
    if row is None:
        raise ValueError(f"Processo {numero_formatado} não existe na tabela processos")
    processo_id = row[0]

    conn.execute(
        text("DELETE FROM movimentos WHERE processo_id = :id"),
        {"id": processo_id},
    )

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

    df.to_sql(
            "movimentos", conn, if_exists="append", index=False,
            dtype={"dados_json": JSONB},  
        )