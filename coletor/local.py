"""Gera o feed /eleicoes/ao-vivo numa pasta local, sem AWS (não vai para a Lambda).

Uso (da raiz do repo):
  uv run --no-project --python 3.14 python coletor/local.py --saida /tmp/feed
  uv run --no-project --python 3.14 python coletor/local.py --saida /tmp/feed --sem-brasil
  uv run --no-project --python 3.14 python coletor/local.py --saida /tmp/feed \\
      --turno 2 --pleito 3221 --eleicoes 6258,6260

Padrão: 1º turno oficial (pleito 3220, eleição 6257), com dados reais do TSE.
Os arquivos saem em <saida>/<prefixo>; o estado do ciclo lento em
<saida>/_controle. Rodar de novo na mesma pasta reaproveita o histórico.
"""
import argparse
import json
import os
import sys
import time
import types
from pathlib import Path


def argumentos():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--saida", required=True, help="pasta onde gravar os arquivos")
    p.add_argument("--base-url", default="https://resultados.tse.jus.br")
    p.add_argument("--ambiente", default="oficial")
    p.add_argument("--turno", default="1")
    p.add_argument("--pleito", default="3220")
    p.add_argument("--eleicoes", default="6257", help="vazio = todas do pleito/turno")
    p.add_argument("--uf", default="ma")
    p.add_argument("--prefixo", default="aovivo/2026/t2/")
    p.add_argument("--base-feed", default="https://d3nthtia8j7edn.cloudfront.net", help="base das URLs das fotos")
    p.add_argument("--cores", default="", help='ex.: {"13":"#C0392B","22":"#2543F4"}')
    p.add_argument("--aviso", default="")
    p.add_argument("--sem-brasil", action="store_true", help="não gera municipios-br.json (5.571 downloads)")
    return p.parse_args()


def main():
    args = argumentos()
    # configuração vem do ambiente na importação dos módulos, como na Lambda
    os.environ.update({
        "BUCKET_NAME": "local", "TSE_BASE_URL": args.base_url, "TSE_AMBIENTE": args.ambiente,
        "TSE_TURNO": args.turno, "TSE_PLEITO": args.pleito, "TSE_ELEICOES": args.eleicoes, "TSE_UF": args.uf,
        "FEED_PREFIXO": args.prefixo, "FEED_BASE_URL": args.base_feed, "FEED_CORES": args.cores,
        "FEED_AVISO": args.aviso,
    })
    try:
        import boto3  # noqa: F401
    except ImportError:
        # sem AWS aqui: o boto3 só precisa existir para o import do coletor
        botocore = types.ModuleType("botocore")
        excecoes = types.ModuleType("botocore.exceptions")
        excecoes.ClientError = type("ClientError", (Exception,), {})
        excecoes.BotoCoreError = type("BotoCoreError", (Exception,), {})
        botocore.exceptions = excecoes
        sys.modules.update({"boto3": types.SimpleNamespace(client=lambda *a, **k: None),
                            "botocore": botocore, "botocore.exceptions": excecoes})
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import feed
    import index as coletor

    def config_direto(prazo=None):
        url = f"{coletor.TSE_BASE_URL}/{coletor.AMBIENTE}/comum/config/ele-c.json"
        return coletor.baixar_json(url, prazo)[0]

    destino = feed.DestinoPasta(args.saida)
    rapido = feed.FeedRapido(destino, feed.ClienteTSE(), config_direto)
    resultado = rapido.executar_ciclo(time.monotonic() + 60)
    print(json.dumps({"ciclo_rapido": resultado}, ensure_ascii=False, indent=2))
    if resultado["status"] != "gravado":
        return 1

    if not args.sem_brasil:
        municipios = feed.FeedMunicipios(destino, feed.ClienteTSE(), config_direto)
        resultado = municipios.executar_com_log(time.monotonic() + 600)
        print(json.dumps({"ciclo_municipios": resultado}, ensure_ascii=False, indent=2))
        if resultado["status"] != "gravado":
            return 1

    pasta = Path(args.saida) / feed.FEED_PREFIXO
    for arquivo in sorted(pasta.glob("*.json")):
        print(f"{arquivo}  ({arquivo.stat().st_size / 1024:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
