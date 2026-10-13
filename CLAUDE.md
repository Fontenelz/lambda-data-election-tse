# Coletor TSE (AWS) — contexto para o Claude

Lambda `coletor-tse` (sa-east-1) espelha arquivos do TSE no bucket `raw-tse-data-2026`,
servidos pelo CloudFront `E3QGP1X906YZKI` (https://d3nthtia8j7edn.cloudfront.net) para
páginas do imirante.com **que estão no ar**. Visão geral, operação e alarmes: `README.md`.

## Regras

- **Não quebrar o que está no ar**: espelho `{ambiente}/{ciclo}/{eleicao}/dados/{uf}/...`,
  `dados_eleicao.json` (mesmo formato), lock, retries, logs JSON com `erro_*`, alarmes, Telegram.
- **Nunca rodar `deploy.sh`, `aws lambda update-*`, `set-alarm-state` ou qualquer escrita na AWS
  sem aprovação explícita do usuário.** O `deploy.sh` publica dois stacks: `coletor-tse`
  (sa-east-1) e `coletor-tse-borda` (us-east-1, alarmes do CloudFront).
- Antes de qualquer deploy, conferir que o repo bate com produção (ver "Fonte de verdade").
- Recursos regionais só em sa-east-1; us-east-1 apenas para CloudFront/alarmes dele. Nada de
  Lambda@Edge, StackSets ou cross-Region. "Access Denied" do nada → perguntar sobre spend limit.
- Código sem dependências externas no runtime (só stdlib + boto3 do Lambda), em português,
  no estilo dos arquivos existentes. Erros sempre via `registrar_erro` (campos `erro_*`).

## Fonte de verdade

Em 2026-10-13 o código implantado (Lambda modificada em 2026-10-03) era o commit `2d1e2d7`
(`origin/coletor-prazo-paralelo-testes`) **mais** o patch `TsePleito` que nunca foi commitado.
O `main`/`First commit` (`db0fd17`) é mais antigo que produção — não usar como base.
Para conferir de novo: `aws lambda get-function --query Code.Location` + `aws cloudformation
get-template --template-stage Original` e `diff` com o repo.

## Comandos

```bash
# testes (não há python3 no PATH; usar o Python do uv)
uv run --no-project --python 3.14 python -m unittest discover -s tests -v

# leitura do stack (só leitura)
aws cloudformation describe-stacks --region=sa-east-1 --stack-name coletor-tse
```

No zsh, `R="--region sa-east-1"; aws ... $R` não funciona (sem word splitting): use `--region=sa-east-1`.
O `~/.aws/config` tem `region = us-east-1` como padrão — sempre passar a região.

## Fatos do TSE já confirmados (ambiente oficial)

- `ele-c.json`: `https://resultados.tse.jus.br/oficial/comum/config/ele-c.json`. Lista pleitos de
  **ele2024 antes de ele2026** → o ciclo tem que vir do pleito de cada eleição, nunca de `pl[0]`.
- 1º turno: pleito 3220, eleições 6257 (Presidente), 6259 (estaduais). 2º turno (25/10/2026):
  pleito **3221**, eleições **6258** (Presidente, abr `br`) e **6260** (Governador, abr só
  am/rn/df/es/ac — **MA não tem 2º turno de governador**). O 3221 também tem suplementares e
  consultas populares com `t=1`.
- Arquivos (cargo 1, eleição com 6 dígitos): `dados/br/br-c0001-e{E6}-u.json`,
  `dados/{uf}/{uf}-c0001-e{E6}-u.json` (inclui `zz`), `dados/{uf}/{uf}{cdmun}-c0001-e{E6}-u.json`,
  `config/mun-e{E6}-cm.json` (campo IBGE = `cdi`), `fotos/br/{sqcand}.jpeg`.
- `dados/{uf}/{uf}-e{E6}-ab.json`: por município `s`/`e`/`dt`/`ht`, **sem votos** — serve como
  detector de mudança, não substitui os arquivos de município. Sufixos `-v/-r/-a/-f/-i/-t` = 404.
- Números vêm como string; percentuais com vírgula. `e="s"` também aparece com `st="2º turno"`
  (não significa eleito) — eleito é `st == "Eleito"`.
- TSE devolve `ETag`/`Last-Modified` e responde 304 a GET condicional; `cache-control: max-age=59`.
- 5.571 municípios fora do exterior; `cdi` do TSE bate 1:1 com a API do IBGE.

## Registro de mudanças

Mais recente primeiro. Cada entrada: data, o quê, por quê, como foi verificado.

- **2026-10-13** — Feed `/eleicoes/ao-vivo` implementado (local, *não implantado, não commitado*):
  - `coletor/feed.py` (handlers `handler_rapido`/`handler_municipios`), `coletor/contrato.py` +
    `coletor/esquemas/` (JSON Schema), `coletor/fastjsonschema/` (vendorizado 2.21.1),
    `coletor/municipios_ibge.json` (+ `ferramentas/gerar_municipios.py`), `coletor/local.py` (CLI).
  - `coletor/index.py`: `TSE_ELEICOES` (vazio = como antes) e lock com chave/TTL configuráveis
    (padrão = o de hoje). `tests/test_nao_regressao.py` prova que grava o mesmo que produção.
  - `template.yaml`: parâmetros `TseEleicoes`, `FeedPrefixo`, `FeedRecarregar`, `FeedAviso`, `FeedCores`,
    `FeedScheduleState` (padrão DISABLED), `FeedAlarme` (padrão DISABLED), `FeedParadoSegundos`;
    Lambdas `coletor-tse-feed`/`coletor-tse-feed-municipios`, role, schedules, metric filters,
    alarme `coletor-tse-feed-parado`; BucketPolicy libera `${FeedPrefixo}*`. cfn-lint limpo e
    `validate-template` OK. Sem alias YAML (CloudFormation não aceita).
  - `deploy.sh`: roda os testes antes e publica o mesmo zip nas 3 funções.
  - `notificador/index.py`: alarme do feed com contexto dos logs do feed (`FONTES`); alarmes antigos
    com texto e filtro idênticos aos de produção (teste).
  - Verificado: 88 testes OK; modo local contra o TSE real (6257) bate com o TSE (BR, ZZ, São Luís,
    soma dos municípios + exterior = total BR). Medido: ciclo rápido 247 req/2,5–6s; varredura do
    Brasil 5.600 req/37s/116 MB, seguinte 29 req/1,6s.
- **2026-10-13** — Descobertas no repo do site (`../imirante`, só leitura): contrato em
  `docs/apuracao-ao-vivo-feed.md`, validação em `resources/js/componentes/apuracao-ao-vivo/useFeed.js`;
  `mapa-br.json` tem 5.570 municípios (falta 5101837 Boa Esperança do Norte/MT); checklist do site
  omite `TsePleito=3221`; a página relê `municipios-br.json` a cada ~30s (compara seq com o do agora).

- **2026-10-13** — Branch `feat/1-segundo-turno-eleicoes-2026` avançada (fast-forward) de
  `db0fd17` para `2d1e2d7` e aplicado o patch `TsePleito` copiado da Lambda em produção
  (`coletor/index.py`, `notificador/index.py` idênticos byte a byte; `template.yaml` com
  `TsePleito`, defaults do ambiente oficial e `TSE_PLEITO`). Testes ajustados para o patch
  (`TSE_PLEITO` vazio no ambiente de teste, nova assinatura de `preparar_alvo`) e 3 testes
  novos: filtro de pleito, abrangência de outra UF ignorada (6260), ciclo por pleito.
  Verificado: diff com produção + 44 testes OK. *Não commitado.*
- **2026-10-13** — Início do trabalho do 2º turno: feed `/eleicoes/ao-vivo` (contrato v1 em
  `aovivo/2026/t2/`) e parâmetro `TseEleicoes`. Plano aprovado: funções separadas
  `coletor-tse-feed` (rápido) e `coletor-tse-feed-municipios` (lento), cada uma com lock próprio;
  municípios = os 5.571 atuais; `agora.json` regravado a cada ciclo; `nulos = v.tvn`.
