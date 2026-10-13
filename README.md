# Coletor TSE — versão AWS

Porte do `coletor_tse` (Cloud Function, `../main.py`) para AWS, na região **`sa-east-1`**. Mesma lógica de coleta, detecção de mudança (delta) e caminho espelhado do TSE; o site passa a ler pelo CloudFront.

```
TSE ──GET──▶ Lambda coletor-tse ──PutObject──▶ S3 raw-tse-data-2026 (privado)
                 ▲                                      │ OAC (SigV4)
     EventBridge Scheduler (1 min)                      ▼
                                              CloudFront (cache 30s, CORS)
                                                        │
                                                        ▼
                                                  site / usuários
```

| GCP / Cloudflare | AWS |
|---|---|
| Cloud Function `coletor_tse` | Lambda `coletor-tse` (Python, arm64, 256 MB, timeout 120s) |
| Cloud Scheduler (OIDC) | EventBridge Scheduler `coletor-tse` (sem retry) |
| Bucket GCS `resultados-tse-imirante` | Bucket S3 `raw-tse-data-2026` |
| Worker Cloudflare `proxy-tse` + HMAC | CloudFront + Origin Access Control |
| Cloud Build | `aws/deploy.sh` (CloudFormation + update do código) |

## Arquivos

- `coletor/index.py` — o coletor/espelho (sem dependências externas; `boto3` já vem no runtime)
- `coletor/feed.py` — feed da página `/eleicoes/ao-vivo` (Lambdas `coletor-tse-feed` e `coletor-tse-feed-municipios`, mesmo zip); ver [Feed /eleicoes/ao-vivo](#feed-eleicoesao-vivo)
- `coletor/contrato.py` + `coletor/esquemas/*.schema.json` — validação do contrato v1 do feed (JSON Schema + regras); `coletor/fastjsonschema/` é a biblioteca vendorizada (BSD, sem alterações)
- `coletor/municipios_ibge.json` — nomes oficiais dos 5.571 municípios por código IBGE (gerado por `ferramentas/gerar_municipios.py`)
- `coletor/local.py` — modo CLI: gera o feed numa pasta, sem AWS (não vai para o zip)
- `template.yaml` — toda a infraestrutura (CloudFormation, `sa-east-1`)
- `template-borda.yaml` — alarmes do CloudFront (stack `coletor-tse-borda` em `us-east-1`, onde ficam as métricas do CloudFront); publicado pelo `deploy.sh`
- `notificador/index.py` — repassa os alarmes para o Telegram
- `deploy.sh` — publica infraestrutura e código
- `tests/` — testes unitários do coletor, do feed, do contrato, do notificador e da CloudFront Function (esta roda no `node`, se instalado), não regressão do espelho contra a cópia de produção (`tests/legado/`) e fixtures reais do 1º turno (`tests/fixtures/tse`, baixadas por `ferramentas/baixar_fixtures.sh`); não vão para o zip da Lambda
- `CLAUDE.md` — contexto do projeto e registro de mudanças

## Testes

Rodam com o Python puro, sem AWS nem `boto3` instalado (o boto3 é trocado por mocks):

```bash
python3 -m unittest discover -s tests -v
# sem python3 no PATH (ex.: só uv instalado):
uv run --no-project --python 3.14 python -m unittest discover -s tests -v
```

O `deploy.sh` roda os testes antes de publicar e para se algum falhar (`SEM_TESTES=1 aws/deploy.sh ...` pula, só em emergência).

## Diferenças em relação à versão GCP

Já incorpora as correções da análise técnica:

- **Falha de download de um cargo não some com ele do índice** — o item anterior é mantido.
- **Execuções sobrepostas são bloqueadas** por um lock no S3 (`_controle/coletor.lock`, escrita condicional). Lock com mais de 180s é considerado abandonado.
- **Retry com backoff** (3 tentativas) em timeout, erro de rede, HTTP 429/5xx e JSON incompleto do TSE.
- **Retry limitado ao tempo da execução** — com o TSE lento, as tentativas somadas passariam do timeout de 120s; a Lambda morreria sem liberar o lock e as execuções seguintes seriam puladas por até 3 min. Agora o timeout de cada requisição encolhe para caber no tempo restante (menos `MARGEM_SEGUNDOS`, padrão 10s, reservados para o S3 e o lock) e não há nova tentativa sem tempo. O cargo que não coube vira falha parcial (`erro_motivo`: *acabou o tempo da execução*).
- **Cargos baixados em paralelo** (`DOWNLOADS_PARALELOS`, padrão 5) — a execução leva o tempo do cargo mais lento, não a soma. Gravação no S3 e ordem do índice continuam em série.
- **`ele-c.json` em cache por 1h** (`CONFIG_TTL_SEGUNDOS`) — em vez de 1440 requisições/dia ao TSE, ~24. Ordem: memória da Lambda quente → cópia em `_controle/config-<TseAmbiente>.json` no S3 (vale após cold start; guarda a URL de origem, então trocar `TseBaseUrl` não reaproveita a cópia) → TSE. Se o TSE falhar ao renovar, usa a cópia antiga (WARNING `TSE indisponível para o ele-c.json ... usando cópia`) em vez de parar a coleta. Se a cópia não tiver eleição do `TseTurno` configurado (ex.: troca para o 2º turno), busca no TSE na hora. Para forçar a renovação: apague o arquivo em `_controle/` e rode `aws/deploy.sh`.
- **Erro ao gravar um cargo no S3 não derruba a coleta** — conta como falha daquele cargo (item anterior mantido).
- **Falha total vira erro da Lambda** (config do TSE indisponível ou nenhum cargo coletado), o que dispara o alarme `coletor-tse-erros`. Falha parcial retorna `status: partial` e loga `WARNING`.
- **Logs em JSON com nível** — dá para filtrar por `ERROR`/`WARNING` de verdade. Todo erro traz `erro_etapa`, `erro_tipo`, `erro_cargo` e `erro_motivo` (em português), que o notificador usa para explicar o alarme no Telegram.
- **`TSE_TURNO` é usado**: só coleta eleições cujo campo `t` do `ele-c.json` bate com o turno. Turno e códigos de eleição/cargo são comparados como texto, então funcionam se o TSE mandar número ou string.
- `Cache-Control: public, max-age=0, s-maxage=30, stale-while-revalidate=60`.

## Deploy

Pré-requisitos: AWS CLI logada e `zip`.

O `deploy.sh` publica dois stacks: `coletor-tse` (`sa-east-1`, recebe os parâmetros abaixo) e depois `coletor-tse-borda` (`us-east-1`, alarmes do CloudFront), que pega distribuição, notificador e `AlertEmail` do primeiro — não precisa passar nada para ele.

```bash
aws/deploy.sh                                        # só atualiza código/infra, mantém parâmetros
aws/deploy.sh ScheduleExpression="rate(1 minute)"    # apuração
aws/deploy.sh TseBaseUrl=https://... TseAmbiente=... # troca para o ambiente oficial
aws/deploy.sh AlertEmail=voce@exemplo.com            # notificação por e-mail dos alarmes
aws/deploy.sh TelegramChatId=-100123...              # notificação no Telegram (ver "Alertas no Telegram")
aws/deploy.sh DadosParadosSegundos=900               # janela do alarme de dados parados
aws/deploy.sh AllowedOrigins="https://imirante.com,https://m.imirante.com,http://homologacao.imirante.com,https://homologacao.imirante.com" # quem pode ler (lista completa)
aws/deploy.sh TseEleicoes=6258,6260                  # só estas eleições (vazio = todas do pleito/turno)
aws/deploy.sh FeedScheduleState=ENABLED              # liga o feed /eleicoes/ao-vivo (ver seção do feed)
```

Parâmetros não informados mantêm o valor atual do stack — só passe o que quer mudar.

## Feed /eleicoes/ao-vivo

Página `/eleicoes/ao-vivo` do imirante.com (contrato v1 em `imirante/docs/apuracao-ao-vivo-feed.md`). Lê 4 arquivos agregados em `https://d3nthtia8j7edn.cloudfront.net/<FeedPrefixo>` (output `FeedUrl`; é o `APURACAO_FEED_URL` do site):

| Arquivo | Conteúdo | Gravado por | Cache-Control |
|---|---|---|---|
| `agora.json` | Presidente: Brasil, 27 UFs + exterior (`ZZ`), candidatos | `coletor-tse-feed`, a cada ~15s | `max-age=5, s-maxage=5, swr=30, sie=600` |
| `municipios-ma.json` | Presidente nos 217 municípios de `TseUf` | `coletor-tse-feed`, a cada ~15s | `max-age=10, s-maxage=5, swr=30, sie=600` |
| `historico.json` | curva da apuração (ponto novo só quando `totalizadas` do BR ou MA muda; até 600 pontos, mantendo o 1º) | `coletor-tse-feed`, a cada ~15s | `max-age=5, s-maxage=5, swr=30, sie=600` |
| `municipios-br.json` | Presidente nos 5.571 municípios (colunar, ordenado por código IBGE; sem exterior) | `coletor-tse-feed-municipios`, a cada ~1 min | `max-age=10, s-maxage=5, swr=30, sie=600` |
| `fotos/<sqcand>.jpeg` | fotos dos candidatos, copiadas uma vez do TSE | `coletor-tse-feed` | `max-age=86400` |

**Como funciona**

- **Funções e locks próprios** (`_controle/feed.lock`, `_controle/feed-municipios.lock`): o espelho e o feed não dependem um do outro; a varredura lenta dos municípios nunca atrasa o ciclo rápido.
- **Ciclo rápido** (`coletor-tse-feed`, 512 MB, timeout 60s): agendada a cada minuto, faz ciclos a cada 15s até 45s (4 por invocação). Cada ciclo baixa `br` + 28 UFs + 217 municípios do MA (~246 arquivos, 16 threads, timeout de 5s por arquivo, conexão persistente, gzip, `If-None-Match`/`If-Modified-Since`). Grava `municipios-ma.json` → `historico.json` → `agora.json` (**agora por último**: quem vê um `seq` novo encontra os outros já gravados). `seq` = epoch em ms do ciclo, igual nos três. Grava **a cada ciclo**, mesmo sem mudança no TSE (`gerado` e `seq` andam; a página detecta coletor parado pelo `seq`).
- **Ciclo lento** (`coletor-tse-feed-municipios`, 512 MB, timeout 120s): baixa os 27 `{uf}-e{eleição}-ab.json` (por município: seções totalizadas e horário, sem votos) e só baixa o arquivo de município que mudou. Último valor bom + ETag de cada município ficam em `_controle/feed-municipios-<eleição>.json`.
- **Falhas**: arquivo do TSE que falha usa o último valor bom (memória → arquivos já publicados, na Lambda fria), **nunca zero**; só entra zerado o que nunca foi baixado. Se o arquivo do **Brasil** falhar, o ciclo não grava nada. UF sem nenhum valor anterior também impede a gravação.
- **Contrato**: os arquivos passam por `coletor/contrato.py` (JSON Schema + `totalizadas <= secoes`, votos de todos os candidatos, arrays do mesmo tamanho, 217/5.571 municípios, `seq` igual) **antes** de gravar; se falhar, nada é gravado e o anterior continua no ar (`erro_etapa: feed: validação do contrato`).
- **Campos**: números do TSE convertidos para inteiros; `nulos` = `v.tvn` (inclui nulos técnicos); `eleito` = candidato com `e="s"` **e** `st` "Eleito"/"Matematicamente eleito" (no 1º turno, `e="s"` também marca quem foi ao 2º turno); `situacao` = `nao-iniciada` (0 totalizadas), `apurando`, `encerrada` (todas); `fonte_atualizado` = `dt`+`ht` do arquivo do Brasil; `nome` dos municípios vem da tabela do IBGE (o TSE manda em maiúsculas).
- **Municípios**: código IBGE = campo `cdi` do `mun-e{eleição}-cm.json` do TSE (casa 1:1 com a API do IBGE — teste em `tests/test_contrato.py`). São **5.571**: o `mapa-br.json` do site tem 5.570 — falta Boa Esperança do Norte (MT, `5101837`), que fica sem desenho no mapa, mas os votos dele entram nos totais.
- **Governador** (6260) não entra no feed v1 — continua só no espelho/índice. No 2º turno de 2026 o MA nem tem governador (6260 só tem AM, RN, DF, ES e AC).

**Parâmetros do stack**

| Parâmetro | Padrão | Uso |
|---|---|---|
| `TseEleicoes` | vazio | Códigos de eleição (vírgula). Trava extra contra suplementares/consultas do mesmo pleito. Vale para espelho e feed |
| `FeedPrefixo` | `aovivo/2026/t2/` | Pasta do feed no bucket; o CloudFront só lê esta pasta (além do espelho) |
| `FeedRecarregar` | vazio (`null`) | **Forçar reload dos leitores**: mude o valor (ex.: `FeedRecarregar=v2`); cada navegador recarrega uma vez, espalhado em 1 min |
| `FeedAviso` | vazio (`null`) | **Aviso em destaque** na página (ex.: `FeedAviso="TSE com instabilidade"`); `FeedAviso=""` tira |
| `FeedCores` | vazio | JSON `{"13":"#C0392B","22":"#2543F4"}`; vazio = a página escolhe |
| `FeedScheduleState` | `DISABLED` | Liga/desliga os dois agendamentos do feed (independente do espelho) |
| `FeedAlarme` | `DISABLED` | Alarme `coletor-tse-feed-parado` (só existe com o feed `ENABLED`) |
| `FeedParadoSegundos` | `180` | Janela sem `agora.json` gravado que dispara o alarme |

`FeedRecarregar`/`FeedAviso` mudam variável de ambiente da Lambda: valem a partir da próxima invocação (até ~1 min) depois do `deploy.sh`.

### Checklist do 2º turno (25/10/2026)

Antes (dias anteriores):

1. Confirmar a Region do projeto em **AWS Settings > View all projects > Overview > Additional Info > Region** (`sa-east-1`) e o **limite de gastos** em **AWS Settings > Billing** (um limite baixo pausa o projeto no meio da apuração — ver custo abaixo).
2. Conferir no `ele-c.json` oficial que 6258/6260 continuam no pleito 3221 com `t=2`.

No dia (de manhã, para o feed já estar no ar quando o TSE publicar os arquivos zerados):

```bash
aws/deploy.sh TsePleito=3221 TseTurno=2 TseEleicoes=6258,6260 \
  TseBaseUrl=https://resultados.tse.jus.br TseAmbiente=oficial \
  ScheduleExpression="rate(1 minute)" ScheduleState=ENABLED \
  FeedPrefixo=aovivo/2026/t2/ FeedScheduleState=ENABLED FeedAlarme=ENABLED
```

> ⚠️ **`TsePleito=3221` é obrigatório**: com o `3220` do 1º turno o coletor não acha nenhuma eleição do 2º turno (o checklist em `imirante/docs/apuracao-ao-vivo-feed.md` não tem esse parâmetro).

Depois: `aws logs tail --region=sa-east-1 /aws/lambda/coletor-tse-feed --since 5m` deve mostrar `Feed gravado | seq ... | pst BR ...` a cada ~15s, e `curl -s -H "Origin: https://imirante.com" https://d3nthtia8j7edn.cloudfront.net/aovivo/2026/t2/agora.json | jq '{seq, turno, fonte_atualizado}'` responder. Se o TSE ainda não publicou os arquivos do 2º turno, o feed loga `TSE respondeu 404` e não grava — normal até a publicação (deixe `FeedAlarme=DISABLED` até lá para não alarmar).

### Operação do feed

```bash
R=--region=sa-east-1
aws/deploy.sh FeedScheduleState=DISABLED            # pausar o feed (espelho segue)
aws/deploy.sh FeedScheduleState=ENABLED             # retomar
aws/deploy.sh FeedRecarregar=$(date +%s)            # forçar reload da página nos leitores
aws/deploy.sh FeedAviso="Apuração atrasada no TSE"  # aviso em destaque; FeedAviso="" remove
aws lambda invoke $R --function-name coletor-tse-feed /dev/stdout              # rodar já
aws logs tail $R /aws/lambda/coletor-tse-feed --follow                         # ao vivo
aws logs tail $R /aws/lambda/coletor-tse-feed-municipios --since 10m           # varredura do Brasil
```

Modo local (sem AWS; dados reais do TSE, 1º turno por padrão):

```bash
uv run --no-project --python 3.14 python coletor/local.py --saida /tmp/feed              # 4 arquivos
uv run --no-project --python 3.14 python coletor/local.py --saida /tmp/feed --sem-brasil # sem os 5.571
```

**Testes na AWS sem afetar o 2º turno**: rode com outro prefixo (ex.: `aws/deploy.sh FeedPrefixo=aovivo/teste/t1/ FeedScheduleState=ENABLED`, mantendo os parâmetros do TSE; **não** use `TseEleicoes` para isso — ele também filtra o espelho e tiraria cargos do `dados_eleicao.json`) e apague a pasta depois — o histórico e o estado são por prefixo/eleição, mas o `historico.json` do prefixo final deve começar limpo no dia (`aws s3 rm --recursive s3://raw-tse-data-2026/aovivo/teste/`).

### Medições (1º turno oficial, 6257, 12 candidatos; a confirmar na Lambda)

| | Ciclo rápido | Varredura do Brasil |
|---|---|---|
| Requisições ao TSE | 247 por ciclo (~990/min com 4 ciclos); 2º ciclo em diante: GET condicional (304) | 1ª: ~5.600; seguintes: 29 + só os municípios que mudaram (pico teórico ~5.600/min no meio da apuração) |
| Duração | 2,5–6s por ciclo | 1ª: 37s; sem mudança: 1,6s |
| Memória | — | 116 MB (pico) |

Pico de requisições ao TSE na apuração: ~1.000/min (rápido) + até ~5.600/min (lento) + ~5/min do espelho.

### Concorrência (limite da conta: 10)

Cada função agendada tem no máximo 2 execuções ao mesmo tempo (uma rodando + uma que acha o lock ocupado e sai em < 1s): espelho 2 + feed 2 + municípios 2 = **6**, mais o notificador (1 por alarme, até 3 juntos) = **9 no pior caso**. Normal: 3–4. Evite `aws lambda invoke` manual durante incidente.

### Custo estimado da noite (preços sa-east-1 / América do Sul, Price List API em 2026-10-13)

- **AWS (coletor)**: Lambda arm64 $0,0000133/GB-s → feed ~1.500 GB-s/h + municípios ~1.200 GB-s/h ≈ **$0,04/h**; S3 PUT ($0,007/1.000) ~1.100/h ≈ $0,01/h; logs < $0,05 na noite. **Menos de $1 na noite toda.**
- **CloudFront (leitores)** — o que pesa: $0,0220/10.000 requisições HTTPS + $0,110/GB. Por leitor com a página aberta, por hora: ~540 requisições + ~6–10 MB (gzip; `municipios-br.json` ~64 KB é ~75% do volume) ≈ **$0,002–0,003 por leitor-hora**. Ex.: 10 mil leitores simultâneos por 4h ≈ **$80–120**; 50 mil ≈ $400–600.
- Para reduzir: a página relê `municipios-br.json` a cada ~30s porque compara o `seq` dele com o do `agora.json` (que nunca são iguais); comparar com o último `seq` do próprio `municipios-br.json` cortaria cerca de metade desse tráfego (mudança no repo do site).


```bash
R=--region=sa-east-1

# rodar uma coleta agora
aws lambda invoke $R --function-name coletor-tse /dev/stdout

# logs (só erros/avisos)
aws logs tail $R /aws/lambda/coletor-tse --since 1h --filter-pattern '{ $.level = "ERROR" || $.level = "WARNING" }'

# pausar / retomar o agendamento
aws/deploy.sh ScheduleState=DISABLED

# ver o índice publicado
curl -s "$(aws cloudformation describe-stacks $R --stack-name coletor-tse \
  --query "Stacks[0].Outputs[?OutputKey=='IndexUrl'].OutputValue" --output text)"
```

Os arquivos individuais ficam em `https://d3nthtia8j7edn.cloudfront.net/<campo arquivo do índice>`, igual ao exemplo de consumo do `README.md` principal trocando `BUCKET_BASE`.

## 📡 Observabilidade e acompanhamento

Referência rápida (conta `281466185599`, região `sa-east-1`):

| Recurso | Nome / ID |
|---|---|
| Stack CloudFormation | `coletor-tse` |
| Lambda | `coletor-tse` |
| Log group | `/aws/lambda/coletor-tse` (retenção 30 dias) |
| Agendamento | EventBridge Scheduler `coletor-tse` — `rate(1 minute)` |
| Lambdas do feed | `coletor-tse-feed` (rápido), `coletor-tse-feed-municipios` (municípios do Brasil) — log groups `/aws/lambda/coletor-tse-feed*` |
| Alarmes | `coletor-tse-erros`, `coletor-tse-dados-parados`, `coletor-tse-feed-parado` (`sa-east-1`); `coletor-tse-cloudfront-4xx`, `coletor-tse-cloudfront-5xx` (`us-east-1`, stack `coletor-tse-borda`) |
| Tópico de alertas | SNS `coletor-tse-alertas` (`sa-east-1`) e `coletor-tse-alertas-cloudfront` (`us-east-1`) — os dois acionam o mesmo notificador |
| Métricas customizadas | `ColetorTSE/ColetasCompletas`, `ColetorTSE/FeedGravado`, `ColetorTSE/FeedMunicipiosGravado` |
| Notificador | Lambda `coletor-tse-telegram` (só com Telegram configurado) |
| Bucket | `raw-tse-data-2026` |
| CloudFront | `E3QGP1X906YZKI` — `https://d3nthtia8j7edn.cloudfront.net` |
| CloudFront Function | `coletor-tse-somente-imirante` |

### Links dos dados publicados

> ⚠️ Abrir direto no navegador retorna **403** — a CloudFront Function só libera requisições com `Origin`/`Referer` do imirante.com. Para inspecionar, use `curl` com o cabeçalho (exemplos abaixo) ou o console do S3.

| Arquivo | URL pública (CloudFront) |
|---|---|
| Índice | https://d3nthtia8j7edn.cloudfront.net/dados_eleicao.json |
| Presidente | https://d3nthtia8j7edn.cloudfront.net/simulado2026/ele2026/21270/dados/br/br-c0001-e021270-u.json |
| Governador (MA) | https://d3nthtia8j7edn.cloudfront.net/simulado2026/ele2026/21272/dados/ma/ma-c0003-e021272-u.json |
| Senador (MA) | https://d3nthtia8j7edn.cloudfront.net/simulado2026/ele2026/21272/dados/ma/ma-c0005-e021272-u.json |
| Deputado Federal (MA) | https://d3nthtia8j7edn.cloudfront.net/simulado2026/ele2026/21272/dados/ma/ma-c0006-e021272-u.json |
| Deputado Estadual (MA) | https://d3nthtia8j7edn.cloudfront.net/simulado2026/ele2026/21272/dados/ma/ma-c0007-e021272-u.json |

Os caminhos seguem `{TseAmbiente}/{ciclo}/{codEleicao}/dados/{uf}/...` — ao trocar para o ambiente oficial (`TseAmbiente`/`TseBaseUrl`) ou para o 2º turno, os códigos mudam. **A fonte de verdade é sempre o campo `arquivo` do índice**; esta tabela reflete o simulado de 2026-09-23.

```bash
BASE=https://d3nthtia8j7edn.cloudfront.net
H="Origin: https://imirante.com"

# quando o índice foi regravado e a geração de cada cargo no TSE
curl -s -H "$H" $BASE/dados_eleicao.json | jq '{atualizado_em, total, cargos: [.resultados[] | {nome_cargo, gerado_em}]}'

# baixar um cargo
curl -s -H "$H" $BASE/simulado2026/ele2026/21272/dados/ma/ma-c0003-e021272-u.json | jq . | head

# conferir cache do CloudFront (x-cache: Hit/Miss, age)
curl -sI -H "$H" $BASE/dados_eleicao.json | grep -iE 'x-cache|age|cache-control|last-modified'
```

### Consoles

**Lambda `coletor-tse` — métricas** · [abrir](https://sa-east-1.console.aws.amazon.com/lambda/home?region=sa-east-1#/functions/coletor-tse?tab=monitoring)
Invocações, erros, duração, throttles e concorrência. É o primeiro lugar para ver se o coletor está rodando.

**Logs do coletor** · [abrir log group](https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1#logsV2:log-groups/log-group/$252Faws$252Flambda$252Fcoletor-tse) · [abrir Logs Insights](https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1#logsV2:logs-insights)
Uma linha JSON por evento, com campo `level` (`INFO`/`WARNING`/`ERROR`). Em incidente, filtre por `ERROR`/`WARNING` primeiro (consultas prontas abaixo).

**Alarme `coletor-tse-erros`** · [abrir](https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1#alarmsV2:alarm/coletor-tse-erros)
Dispara quando a métrica `Errors` da Lambda é ≥ 1 em 5 minutos (config do TSE indisponível, nenhum cargo coletado ou exceção).

**Alarme `coletor-tse-dados-parados`** · [abrir](https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1#alarmsV2:alarm/coletor-tse-dados-parados)
Dispara quando passa `DadosParadosSegundos` (padrão 10 min) **sem nenhuma coleta completa**. Pega o que o alarme de erros não vê: coletor que parou de rodar, lock preso (execuções puladas), falhas seguidas e cargo que falha toda vez (coleta `partial`). A métrica `ColetorTSE/ColetasCompletas` vem de um filtro no log (`coleta_status = "success"`). O alarme só existe com o agendamento `ENABLED`. Ele **não** dispara quando o TSE simplesmente não publicou nada novo: coleta sem mudança também é completa.

**Alarme `coletor-tse-feed-parado`** · [abrir](https://sa-east-1.console.aws.amazon.com/cloudwatch/home?region=sa-east-1#alarmsV2:alarm/coletor-tse-feed-parado)
Dispara quando passa `FeedParadoSegundos` (padrão 3 min) **sem nenhum `agora.json` gravado** (métrica `ColetorTSE/FeedGravado`, filtro `feed_status = "gravado"` no log do `coletor-tse-feed`). Só existe com `FeedScheduleState=ENABLED` e `FeedAlarme=ENABLED`. No Telegram, a mensagem traz os ciclos gravados/com erro na janela, os erros agrupados (`erro_etapa` começa com `feed:`) e o que fazer.

Os alarmes avisam quando disparam e quando voltam ao normal, pelo tópico SNS `coletor-tse-alertas` (Telegram e/ou e-mail).

**Agendamento (EventBridge Scheduler)** · [abrir](https://sa-east-1.console.aws.amazon.com/scheduler/home?region=sa-east-1#schedules/default/coletor-tse)
Mostra se está `ENABLED` e a frequência. Sem retry: execução perdida só é compensada pela próxima.

**Bucket `raw-tse-data-2026`** · [abrir objetos](https://sa-east-1.console.aws.amazon.com/s3/buckets/raw-tse-data-2026?region=sa-east-1&tab=objects)
Fonte de verdade: `dados_eleicao.json`, os arquivos espelhados do TSE e o lock em `_controle/coletor.lock` (se ele existir por muito tempo, veja *Lock abandonado* abaixo). A coluna *Last modified* mostra quando cada cargo foi regravado.

**CloudFront — métricas** · [abrir](https://us-east-1.console.aws.amazon.com/cloudfront/v4/home?region=us-east-1#/monitoring/distribution/E3QGP1X906YZKI) · [distribuição](https://us-east-1.console.aws.amazon.com/cloudfront/v4/home?region=us-east-1#/distributions/E3QGP1X906YZKI)
Requisições, bytes entregues, taxa de 4xx/5xx e cache hit rate. CloudFront é global: as métricas ficam em **`us-east-1`**, não em `sa-east-1`.

**CloudFront Function `coletor-tse-somente-imirante`** · [abrir](https://us-east-1.console.aws.amazon.com/cloudfront/v4/home?region=us-east-1#/functions/coletor-tse-somente-imirante)
Invocações e erros de execução do bloqueio por origem. A aba de teste permite simular um `Origin` e ver se é liberado.

**Stack CloudFormation** · [abrir](https://sa-east-1.console.aws.amazon.com/cloudformation/home?region=sa-east-1#/stacks?filteringText=coletor-tse)
Parâmetros atuais (ambiente TSE, turno, UF, frequência, origens), outputs e histórico de deploys.

### Métricas que importam

| Métrica | Onde | Esperado | Sinal de problema |
|---|---|---|---|
| `Invocations` | Lambda | ~1 por minuto | Zero → agendamento desativado ou quebrado |
| `Errors` | Lambda | 0 | ≥ 1 → falha total da coleta (dispara o alarme) |
| `Duration` | Lambda | poucos segundos | Perto de 120s → TSE lento; retries estão acontecendo |
| `Throttles` | Lambda | 0 | > 0 → limite de concorrência da conta (10) estourado |
| `Requests` | CloudFront | acompanha o tráfego do site | Queda brusca em horário de pico → site não está buscando |
| `5xxErrorRate` | CloudFront | ~0% | > 0 → problema na origem S3 / OAC |
| `4xxErrorRate` | CloudFront | baixo | Alto → acessos bloqueados (403 da função) ou arquivo inexistente (404) |
| `CacheHitRate` | CloudFront | alto em pico | Baixo → cabeçalhos de cache errados |

> `CacheHitRate` e os detalhes de 4xx/5xx por código vêm das *additional metrics* do CloudFront, habilitadas pelo stack (`MonitoringSubscription`; cobradas como métricas customizadas do CloudWatch).

**Alarmes do CloudFront** (`us-east-1`, janelas de 5 min, ignorados com menos de 100 requisições na janela):

| Alarme | Dispara quando | Causa mais comum |
|---|---|---|
| `coletor-tse-cloudfront-4xx` | 4xx > 5% por 10 min | site numa origem fora de `AllowedOrigins` (ex.: `www.`) → 403 da função |
| `coletor-tse-cloudfront-5xx` | 5xx > 1% em 5 min | S3/OAC ou CloudFront Function com erro |

Limites ajustáveis nos parâmetros `Limite4xxPercent`, `Limite5xxPercent` e `MinimoRequisicoes` de `template-borda.yaml`.

### Mensagens de log e o que significam

| Nível | Mensagem | Significado |
|---|---|---|
| INFO | `Coleta finalizada: {...}` | Resumo da execução; `status` = `success` ou `partial` (com `cargos_com_erro`) |
| INFO | `Configuração (memória \| cópia no S3 \| TSE) \| ciclo ele2026` | De onde veio o `ele-c.json` nesta execução |
| INFO | `Nenhum cargo mudou desde a última coleta` | Normal — o TSE não publicou nada novo; índice não foi regravado |
| INFO | `Atualizado ('...' -> '...')` | Cargo mudou e foi regravado no S3 |
| WARNING | `Tentativa N/3 falhou para <url>: <motivo>` | Timeout/rede/429/5xx/JSON incompleto do TSE; vai tentar de novo |
| WARNING | `Coleta parcial: N cargo(s) com erro: Senador (MA), ...` | Algum cargo falhou; o item anterior foi mantido no índice |
| WARNING | `Outra coleta em andamento — execução ignorada.` | Execução anterior ainda rodando (lock ativo); `status: skipped` |
| WARNING | `TSE indisponível para o ele-c.json (...) — usando cópia de N min atrás` | Renovação do cache falhou; coleta seguiu com a cópia antiga |
| WARNING | `Lock abandonado há Ns, assumindo` | Execução anterior morreu sem liberar o lock (> 180s) |
| ERROR | `download do cargo [Senador (MA)]: TSE instável (HTTP 503)` | Download de um cargo falhou após os retries |
| ERROR | `gravação do cargo no S3 [...]: S3 recusou a operação (...)` | Cargo baixado mas não gravado; item anterior mantido |
| ERROR | `configuração do TSE (ele-c.json): ...` | **Falha total** (`coleta_status: error`): sem a config não há o que coletar |
| ERROR | `coleta dos cargos: nenhum cargo foi coletado ...` | **Falha total**: todos os cargos falharam |
| ERROR | `gravação do índice no S3: ...` / `lock no S3: ...` / `leitura do índice anterior: ...` | **Falha total** por erro no S3 |
| ERROR | `liberação do lock: ...` | Lock ficou no S3; a próxima execução pode ser pulada até ele expirar |
| INFO | `Feed gravado \| seq N \| pst BR 0.4213 \| 0 falha(s) \| 247 req (240 sem mudança) \| 2534 ms` | Ciclo do feed gravou os 3 arquivos (`feed_status: gravado`) |
| WARNING | `Feed: N arquivo(s) do TSE falharam neste ciclo — usando o último valor bom: Presidente (SP), ...` | Feed gravado com o valor anterior desses arquivos |
| ERROR | `feed: download do arquivo do TSE [Presidente (BR)]: ...` | Sem o Brasil o ciclo não grava; a página segue com o anterior |
| ERROR | `feed: validação do contrato: ...` | Arquivo montado não passou no contrato; nada gravado (formato do TSE mudou?) |
| ERROR | `feed: configuração do TSE (ele-c.json): esperava 1 eleição de Presidente ...` | `TsePleito`/`TseTurno`/`TseEleicoes` não batem com o `ele-c.json` |
| INFO | `Feed municípios gravado \| seq N \| 5571 municípios \| ...` | Varredura do Brasil gravou `municipios-br.json` (com duração, requisições e memória) |

Tipos de erro (`erro_tipo`): `tse_indisponivel` (timeout, rede, 429, 5xx), `tse_nao_encontrado` (404, turno/ambiente errado), `tse_conteudo_invalido` (JSON inválido ou formato mudou), `s3`, `inesperado`.

```
# erros por cargo e motivo nas últimas horas (Logs Insights)
filter ispresent(erro_motivo)
| stats count(*) by erro_etapa, erro_cargo, erro_motivo
| sort count(*) desc
```

### Consultas prontas (CloudWatch Logs Insights, log group `/aws/lambda/coletor-tse`)

```
# erros e avisos recentes
fields @timestamp, level, message
| filter level in ["ERROR", "WARNING"]
| sort @timestamp desc
| limit 100
```

```
# resultado de cada execução por hora (success / partial / skipped)
filter message like /Coleta finalizada|execução ignorada/
| parse message '"status": "*"' as status
| fields coalesce(status, "skipped") as st
| stats count(*) by st, bin(1h)
```

```
# quando cada cargo foi atualizado de fato
fields @timestamp, message
| filter message like /Atualizado|Salvo s3/
| sort @timestamp desc
```

Pelo terminal:

```bash
R=--region=sa-east-1
aws logs tail $R /aws/lambda/coletor-tse --follow                     # ao vivo
aws logs tail $R /aws/lambda/coletor-tse --since 1h \
  --filter-pattern '{ $.level = "ERROR" || $.level = "WARNING" }'     # só problemas
aws cloudwatch describe-alarms $R --alarm-names coletor-tse-erros \
  --query 'MetricAlarms[0].[StateValue,StateReason]'                  # estado do alarme
aws scheduler get-schedule $R --name coletor-tse --query '[State,ScheduleExpression]'
```

### Acompanhando se os dados estão frescos

- **`atualizado_em`** (índice) — quando o índice foi regravado. Só muda quando **algum cargo mudou** no TSE, então ficar parado não é necessariamente erro.
- **`gerado_em`** (por cargo) — horário de geração do arquivo no TSE. Compare com o site do TSE: se o TSE já publicou um horário mais novo e o índice não, o coletor está com problema.
- O CloudFront guarda até **30s** de cache (`s-maxage=30`); somando 1 min de agendamento, o atraso normal entre TSE e site é de até ~1,5 min.
- Para diferenciar "TSE não publicou nada" de "coletor parado", veja `Invocations` da Lambda e a mensagem `Nenhum cargo mudou` nos logs.

### Roteiro de diagnóstico

1. **Alerta no Telegram, alarme em `ALARM` ou site desatualizado** → consulta de erros no Logs Insights.
2. **Sem logs recentes** → agendamento `DISABLED`? `Invocations` zerado? `Throttles`?
3. **Logs com `execução ignorada` seguidos** → lock preso: veja `_controle/coletor.lock` no bucket (expira sozinho após 180s).
4. **Coleta OK mas site sem dados** → `curl` com `Origin` na URL do CloudFront; 403 = origem não permitida; 5xx = S3/OAC.
5. **Acessos negados de repente (`AccessDenied`) em operações que funcionavam** → verificar se o limite de gastos do projeto foi atingido em AWS Settings > Billing.

### Alertas no Telegram

Os alarmes chegam num grupo do Telegram por um bot. O token do bot fica no **SSM Parameter Store** (SecureString), nunca no template nem no git.

Quando um alarme dispara, o notificador consulta os logs e a métrica do coletor e monta a mensagem com:

- **o que é** (🔴 coletor falhou / 🟠 dados do site parados) e o impacto no site;
- **execuções na janela do alarme** (completas, parciais, com falha total, puladas por lock, quebradas) — ou "não rodou";
- **erros agrupados** por cargo/etapa e motivo em português (ex.: `3× Senador (MA): TSE instável (HTTP 503)`);
- **última coleta completa** (horário de Brasília e há quanto tempo);
- **o que fazer**, conforme a causa (TSE fora → aguardar; 404 → conferir parâmetros; S3 → permissões/limite de gastos; lock; deploy quebrado);
- links para o alarme e para os logs.

Ao normalizar (✅) informa quanto tempo o problema durou. Se a consulta aos logs falhar, a mensagem básica sai mesmo assim; se o Telegram recusar o HTML, reenvia como texto puro; em 429, espera o `retry_after` e reenvia.

Exemplo:

```
🔴 Coletor TSE falhou

Execuções do coletor terminaram em erro. O site continua com os últimos
dados publicados, que podem ficar desatualizados.

• Execuções em 5 min: 2 parcial(is), 3 com falha total

📋 Erros
• 3× configuração do TSE (ele-c.json): TSE não respondeu em 15s — https://...
• 2× Senador (MA): TSE instável (HTTP 503)

🕒 Última coleta completa: 24/09 09:07 (há 9 min)

🔎 O que fazer: TSE lento ou fora do ar. Não precisa agir: o coletor tenta
de novo a cada execução e normaliza sozinho quando o TSE voltar.

Alarme · Logs do coletor · 24/09 09:16 (Brasília)
```

1. No Telegram, fale com **@BotFather** → `/newbot` → guarde o token (`123456:ABC...`).
2. Crie um grupo (ex.: *Coletor TSE — alertas*), adicione o bot e mande qualquer mensagem no grupo.
3. Descubra o ID do grupo (número negativo, ex.: `-1001234567890`):
   ```bash
   curl -s "https://api.telegram.org/bot<TOKEN>/getUpdates" | jq '.result[].message.chat | {id, title}'
   ```
4. Guarde o token no SSM (uma vez; para trocar, repita com `--overwrite`):
   ```bash
   aws ssm put-parameter --region sa-east-1 --name /coletor-tse/telegram-token \
     --type SecureString --value '<TOKEN>'
   ```
5. Ative:
   ```bash
   aws/deploy.sh TelegramChatId=-1001234567890
   ```
6. Teste o caminho completo (alarme → SNS → Lambda → Telegram) sem esperar uma falha real:
   ```bash
   aws cloudwatch set-alarm-state --region sa-east-1 --alarm-name coletor-tse-erros \
     --state-value ALARM --state-reason "Teste de notificação"
   ```
   O alarme volta sozinho para `OK` na próxima avaliação e manda o ✅.

Se a mensagem não chegar, veja os logs de `/aws/lambda/coletor-tse-telegram`. O erro da API do Telegram aparece ali (bot fora do grupo, `chat_id` errado, token inválido). Para desligar: `aws/deploy.sh TelegramChatId=""`.

### Pendências de observabilidade

- **Sem logs de acesso do CloudFront** — há métricas por código de erro e alarme de 4xx/5xx, mas para ver *quem* foi bloqueado (403) é preciso habilitar o standard logging (configurado a partir de `us-east-1`; caberia no `template-borda.yaml`).
- **Duração/memória por execução** não aparece nos logs (`SystemLogLevel: WARN` filtra as linhas `REPORT`); use a métrica `Duration`.
- **Falha do próprio notificador não avisa ninguém** — se o Telegram cair ou o token for revogado, o erro fica só no log do `coletor-tse-telegram`. Vale ter também `AlertEmail` como segundo canal.
- **TSE parado não é alertado** — se o TSE parar de publicar, o coletor segue "completo". Compare `gerado_em` com o site do TSE durante a apuração.

## Remover tudo

```bash
aws cloudformation delete-stack --region us-east-1 --stack-name coletor-tse-borda
aws cloudformation delete-stack --region sa-east-1 --stack-name coletor-tse
```

O bucket e os dados não são apagados (o bucket não pertence ao stack); só a bucket policy é removida.

## Limitação conhecida

A conta tem limite de 10 execuções concorrentes de Lambda, o que impede usar concorrência reservada = 1 (a AWS exige manter 100 livres). Por isso o controle de sobreposição é o lock no S3. Pedindo aumento da cota *Concurrent executions* em Service Quotas, dá para trocar o lock por `ReservedConcurrentExecutions: 1`.

## Quem pode ler os dados

Somente `https://imirante.com`, `https://m.imirante.com` e `http(s)://homologacao.imirante.com` (parâmetro `AllowedOrigins`; homologação hoje só responde em HTTP), em duas camadas:

1. **CORS** — navegador só entrega a resposta ao JavaScript de páginas do imirante.com.
2. **CloudFront Function** (`coletor-tse-somente-imirante`) — responde **403** na borda a qualquer requisição cujo `Origin` (ou, na falta dele, `Referer`) não seja uma origem permitida. Abrir a URL direto no navegador, embutir em outro site ou `curl` simples são bloqueados. Também responde o **preflight CORS** (`OPTIONS`) com 204 para as origens permitidas — o S3 recusaria com 403, e o navegador bloquearia qualquer `fetch()` que mande cabeçalho próprio (ex.: `Cache-Control`).

Cabeçalhos podem ser forjados por scripts, então isso impede uso por outros sites, mas não é autenticação. Para depurar:

```bash
curl -H "Origin: https://imirante.com" https://<distribuição>/dados_eleicao.json
```
