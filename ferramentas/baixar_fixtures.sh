#!/usr/bin/env bash
# Baixa do TSE (ambiente oficial, 1º turno 2026, eleição 6257) os arquivos usados
# como fixtures em tests/fixtures/tse, com o mesmo caminho do TSE — o servidor
# HTTP dos testes serve essa pasta como se fosse o TSE.
# Uso: ferramentas/baixar_fixtures.sh   (só precisa rodar de novo para atualizar)
set -euo pipefail

BASE=https://resultados.tse.jus.br
DIR="$(cd "$(dirname "$0")/.." && pwd)/tests/fixtures/tse"
ELE=6257; E6=006257; RAIZ=oficial/ele2026/$ELE
UA="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0 Safari/537.36"

baixar() {  # caminho relativo ao TSE
  mkdir -p "$DIR/$(dirname "$1")"
  curl -sSf --compressed -A "$UA" -o "$DIR/$1" "$BASE/$1"
}

baixar oficial/comum/config/ele-c.json
baixar $RAIZ/config/mun-e$E6-cm.json
baixar $RAIZ/dados/br/br-c0001-e$E6-u.json
baixar $RAIZ/dados/br/br-e$E6-ab.json
for uf in $(jq -r '.abr[].cd' "$DIR/$RAIZ/config/mun-e$E6-cm.json"); do
  baixar $RAIZ/dados/$uf/$uf-c0001-e$E6-u.json
done
baixar $RAIZ/dados/ma/ma-e$E6-ab.json
for mu in $(jq -r '.abr[] | select(.cd == "ma") | .mu[].cd' "$DIR/$RAIZ/config/mun-e$E6-cm.json"); do
  baixar $RAIZ/dados/ma/ma$mu-c0001-e$E6-u.json
done
# fotos só dos dois primeiros colocados (o resto do teste não precisa de imagem real)
for sq in $(jq -r '[.carg[0].agr[].par[].cand[]] | sort_by(-(.vap|tonumber)) | .[0:2][] | .sqcand' "$DIR/$RAIZ/dados/br/br-c0001-e$E6-u.json"); do
  baixar $RAIZ/fotos/br/$sq.jpeg
done
du -sh "$DIR"
