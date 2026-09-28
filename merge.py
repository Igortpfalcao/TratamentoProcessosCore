import pandas as pd
import os
from sqlalchemy import create_engine

senhaDB = os.environ.get('DbSenha')
if not senhaDB:
    raise ValueError(
        "Variável não encontrada no ambiente"
    )

engine = create_engine(
    f"postgresql+psycopg2://postgres:{senhaDB}@localhost:5432/ExeJud"
)

df1 = pd.read_csv("processos/TRF1_CPL.csv", encoding="latin1", sep=";")
df2 = pd.read_csv("processos/TRF1_CPL_15anos.csv", encoding="latin1", sep=";")
df3 = pd.read_csv("processos/TJBA_CPL.csv", encoding="latin1", sep=";")
df4 = pd.read_csv("processos/TJBA_CPL_15anos.csv", encoding="latin1", sep=";")

df_final = pd.concat([df1, df2, df3, df4], ignore_index=True)

df_final.to_sql(
    "processos",
    engine,
    if_exists="append",
    index=False
)

