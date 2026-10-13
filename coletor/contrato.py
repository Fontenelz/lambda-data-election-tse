import json
from pathlib import Path

import fastjsonschema

# Validação do feed /eleicoes/ao-vivo (contrato v1) antes de gravar: JSON
# Schema (esquemas/*.schema.json, os mesmos usados nos testes) + regras que o
# Schema não expressa (totalizadas <= secoes, votos de todos os candidatos,
# arrays paralelos do mesmo tamanho, quantidade de municípios, seq igual).
# Qualquer violação levanta ContratoInvalido; quem chama mantém o arquivo
# anterior no ar.

PASTA = Path(__file__).resolve().parent / "esquemas"


class ContratoInvalido(ValueError):
    pass


def _compilar(nome):
    with open(PASTA / f"{nome}.schema.json", encoding="utf-8") as arquivo:
        return fastjsonschema.compile(json.load(arquivo))


_ESQUEMAS = {nome: _compilar(nome) for nome in ("agora", "municipios-uf", "municipios-br", "historico")}


def _schema(nome, doc):
    try:
        _ESQUEMAS[nome](doc)
    except fastjsonschema.JsonSchemaValueException as erro:
        raise ContratoInvalido(f"{nome}: {erro.message}") from None


def _exigir(condicao, motivo):
    if not condicao:
        raise ContratoInvalido(motivo)


def _votos_completos(votos, numeros, onde):
    _exigir(set(votos) == numeros, f"{onde}: votos de {sorted(votos)} != candidatos {sorted(numeros)}")


def _totais(totais, numeros, onde):
    _exigir(totais["totalizadas"] <= totais["secoes"],
            f"{onde}: totalizadas ({totais['totalizadas']}) > secoes ({totais['secoes']})")
    _votos_completos(totais["votos"], numeros, onde)
    _exigir(totais["eleito"] is None or totais["eleito"] in numeros, f"{onde}: eleito fora dos candidatos")


def validar_agora(doc):
    _schema("agora", doc)
    numeros = [c["numero"] for c in doc["candidatos"]]
    _exigir(len(set(numeros)) == len(numeros), "agora: número de candidato repetido")
    numeros = set(numeros)
    _totais(doc["presidente"]["br"], numeros, "agora.br")
    for uf, totais in doc["presidente"]["uf"].items():
        _totais(totais, numeros, f"agora.uf.{uf}")


def validar_municipios_uf(doc, numeros, quantidade):
    _schema("municipios-uf", doc)
    municipios = doc["municipios"]
    _exigir(len(municipios) == quantidade, f"municipios-uf: {len(municipios)} municípios, esperado {quantidade}")
    _exigir(len({m["ibge"] for m in municipios}) == quantidade, "municipios-uf: código IBGE repetido")
    for m in municipios:
        onde = f"municipios-uf.{m['ibge']}"
        _exigir(m["totalizadas"] <= m["secoes"], f"{onde}: totalizadas > secoes")
        _votos_completos(m["votos"], numeros, onde)


def validar_municipios_br(doc, numeros, quantidade):
    _schema("municipios-br", doc)
    _exigir(len(doc["ibge"]) == quantidade, f"municipios-br: {len(doc['ibge'])} municípios, esperado {quantidade}")
    colunas = {"secoes": doc["secoes"], "totalizadas": doc["totalizadas"]}
    if "eleitorado" in doc:
        colunas["eleitorado"] = doc["eleitorado"]
    _votos_completos(doc["votos"], numeros, "municipios-br")
    colunas.update({f"votos.{n}": v for n, v in doc["votos"].items()})
    for nome, coluna in colunas.items():
        _exigir(len(coluna) == quantidade, f"municipios-br: coluna {nome} com {len(coluna)} itens, esperado {quantidade}")
    _exigir(all(t <= s for t, s in zip(doc["totalizadas"], doc["secoes"])), "municipios-br: totalizadas > secoes")


def validar_historico(doc, numeros):
    _schema("historico", doc)
    for i, ponto in enumerate(doc["pontos"]):
        for serie in ("br", "ma"):
            _votos_completos(ponto[serie]["votos"], numeros, f"historico.pontos[{i}].{serie}")


def validar_ciclo(agora, municipios_uf, historico, quantidade_uf):
    # os três arquivos do ciclo rápido, juntos: só grava se todos passarem
    validar_agora(agora)
    numeros = {c["numero"] for c in agora["candidatos"]}
    validar_municipios_uf(municipios_uf, numeros, quantidade_uf)
    validar_historico(historico, numeros)
    _exigir(agora["seq"] == municipios_uf["seq"] == historico["seq"], "seq diferente entre os arquivos do ciclo")
