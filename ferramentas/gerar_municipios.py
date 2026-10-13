"""Gera coletor/municipios_ibge.json: código IBGE (7 dígitos) -> nome oficial.

O código IBGE de cada município vem do próprio TSE (campo `cdi` do
mun-e{ELEICAO}-cm.json); daqui sai só o nome com acentuação/caixa corretas
("São Luís" em vez de "SÃO LUÍS"), da API de localidades do IBGE. Falha se
algum município do TSE não existir no IBGE ou vice-versa.

Uso: python ferramentas/gerar_municipios.py [caminho do mun-...-cm.json]
     (padrão: fixture do 1º turno em tests/fixtures/tse)
"""
import gzip
import json
import sys
import urllib.request
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent
CM_PADRAO = RAIZ / "tests/fixtures/tse/oficial/ele2026/6257/config/mun-e006257-cm.json"
SAIDA = RAIZ / "coletor/municipios_ibge.json"
API_IBGE = "https://servicodados.ibge.gov.br/api/v1/localidades/municipios?view=nivelado"


def main():
    cm = json.loads(Path(sys.argv[1] if len(sys.argv) > 1 else CM_PADRAO).read_text("utf-8"))
    tse = {mu["cdi"]: (abr["cd"], mu["cd"]) for abr in cm["abr"] if abr["cd"] != "zz" for mu in abr["mu"]}

    with urllib.request.urlopen(API_IBGE, timeout=60) as resposta:
        corpo = resposta.read()
    if corpo[:2] == b"\x1f\x8b":  # a API responde em gzip mesmo sem pedir
        corpo = gzip.decompress(corpo)
    ibge = {str(m["municipio-id"]): m for m in json.loads(corpo)}

    so_tse = sorted(set(tse) - set(ibge))
    so_ibge = sorted(set(ibge) - set(tse))
    if so_tse or so_ibge:
        sys.exit(f"Não casam — só no TSE: {so_tse}; só no IBGE: {so_ibge}")
    for cdi, (uf, _) in tse.items():
        if ibge[cdi]["UF-sigla"].lower() != uf:
            sys.exit(f"UF diferente para {cdi}: TSE {uf}, IBGE {ibge[cdi]['UF-sigla']}")

    tabela = {cdi: ibge[cdi]["municipio-nome"] for cdi in sorted(tse)}
    SAIDA.write_text(json.dumps(tabela, ensure_ascii=False, indent=0) + "\n", "utf-8")
    print(f"{len(tabela)} municípios gravados em {SAIDA.relative_to(RAIZ)}")


if __name__ == "__main__":
    main()
