import html
import json
import logging
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

import boto3

# Repassa para um grupo do Telegram os alarmes do CloudWatch publicados no
# tópico SNS coletor-tse-alertas (e coletor-tse-alertas-cloudfront, em
# us-east-1, com os alarmes do CloudFront). O token do bot fica no SSM (SecureString).
# Ao disparar, consulta os logs do coletor (campos erro_* gravados por ele) e a
# métrica de coletas completas para explicar o que aconteceu e o que fazer. Se
# essa consulta falhar, a mensagem básica é enviada mesmo assim.

logger = logging.getLogger()
logger.setLevel("INFO")

CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
TOKEN_PARAM = os.environ["TELEGRAM_TOKEN_PARAM"]
REGION = os.environ["AWS_REGION"]
COLETOR_LOG_GROUP = os.environ.get("COLETOR_LOG_GROUP", "/aws/lambda/coletor-tse")

# Brasil não tem mais horário de verão
BRASILIA = timezone(timedelta(hours=-3), "Brasília")
LIMITE_TELEGRAM = 4096
MAX_LINHAS_ERRO = 8

ssm = boto3.client("ssm")
logs = boto3.client("logs")
cloudwatch = boto3.client("cloudwatch")
_cloudwatch_por_regiao = {REGION: cloudwatch}
_token = None

ALARMES = {
    "coletor-tse-erros": {
        "ALARM": ("🔴", "Coletor TSE falhou",
                  "Execuções do coletor terminaram em erro. O site continua com os últimos dados "
                  "publicados, que podem ficar desatualizados."),
        "OK": ("✅", "Coletor TSE voltou a funcionar", "As execuções voltaram a terminar sem erro."),
    },
    "coletor-tse-dados-parados": {
        "ALARM": ("🟠", "Dados do site parados",
                  "Nenhuma coleta completa em {janela}. O site pode estar mostrando resultados atrasados."),
        "OK": ("✅", "Dados do site voltaram a atualizar", "As coletas completas voltaram."),
    },
    "coletor-tse-cloudfront-4xx": {
        "ALARM": ("🟠", "CloudFront recusando requisições (4xx)",
                  "Parte relevante dos acessos aos dados está recebendo 403/404. Leitores podem estar "
                  "sem resultados, mesmo com o coletor funcionando."),
        "OK": ("✅", "CloudFront voltou a responder normalmente", "A taxa de 4xx voltou ao normal."),
    },
    "coletor-tse-cloudfront-5xx": {
        "ALARM": ("🔴", "CloudFront com erros 5xx",
                  "O CloudFront não está conseguindo entregar os dados ao site."),
        "OK": ("✅", "CloudFront voltou a entregar os dados", "A taxa de 5xx voltou ao normal."),
    },
}

# alarmes do CloudFront: não dependem do coletor, então a mensagem traz a
# dica direto em vez do resumo dos logs dele
DICAS_CLOUDFRONT = {
    "coletor-tse-cloudfront-4xx": "Quase sempre é 403 da CloudFront Function: o site está numa origem fora de "
                                  "AllowedOrigins (ex.: www.imirante.com) ou mudou de domínio. Confira o Origin "
                                  "das requisições no navegador e rode deploy.sh AllowedOrigins=... com a lista "
                                  "completa. 404 indica o site pedindo um arquivo que não existe.",
    "coletor-tse-cloudfront-5xx": "Veja no console do CloudFront (Monitoramento) se o erro é de origem (S3) ou da "
                                  "CloudFront Function; confira também a política do bucket.",
}

DICAS = {
    "tse_indisponivel": "TSE lento ou fora do ar. Não precisa agir: o coletor tenta de novo a cada "
                        "execução e normaliza sozinho quando o TSE voltar.",
    "tse_nao_encontrado": "O arquivo não existe no TSE. Confira os parâmetros TseAmbiente, TseBaseUrl "
                          "e TseTurno (mudou para o ambiente oficial ou 2º turno?).",
    "tse_conteudo_invalido": "O TSE devolveu arquivo incompleto ou em formato diferente. Se persistir, "
                             "o formato pode ter mudado e o coletor precisa de ajuste.",
    "s3": "Falha no S3. Verifique permissões e se o limite de gastos do projeto não foi atingido "
          "(AWS Settings > Billing).",
    "inesperado": "Erro no código do coletor. Veja os logs e o último deploy.",
    "quebrou": "A função quebrou antes de rodar (deploy com problema?). Veja os logs e o último deploy.",
    "lock": "Execuções puladas porque outra ainda estava rodando ou o lock ficou preso. Ele expira "
            "sozinho em 3 min; se persistir, apague _controle/coletor.lock no bucket.",
    "nao_rodou": "O coletor não rodou. Veja se o agendamento está ENABLED "
                 "(aws scheduler get-schedule --name coletor-tse) e se há throttles na Lambda.",
    "parcial": "Algum cargo falha em toda execução; os demais seguem atualizando.",
}

# ordem de prioridade quando há mais de uma causa
PRIORIDADE_DICAS = ["quebrou", "s3", "inesperado", "tse_nao_encontrado", "tse_conteudo_invalido",
                    "tse_indisponivel", "lock", "parcial", "nao_rodou"]


def token():
    global _token
    if _token is None:
        _token = ssm.get_parameter(Name=TOKEN_PARAM, WithDecryption=True)["Parameter"]["Value"]
    return _token


def parse_data(texto):
    # StateChangeTime vem como 2026-09-23T19:14:37.626+0000
    return datetime.strptime(texto, "%Y-%m-%dT%H:%M:%S.%f%z")


def hora(dt):
    return dt.astimezone(BRASILIA).strftime("%d/%m %H:%M")


def duracao(segundos):
    minutos = max(1, round(segundos / 60))
    if minutos < 60:
        return f"{minutos} min"
    return f"{minutos // 60}h{minutos % 60:02d}"


def link_console(fragmento, regiao=REGION):
    return f"https://{regiao}.console.aws.amazon.com/cloudwatch/home?region={regiao}#{fragmento}"


def link_cloudfront():
    return "https://us-east-1.console.aws.amazon.com/cloudfront/v4/home#/monitoring"


def regiao_do_alarme(alarme):
    # AlarmArn: arn:aws:cloudwatch:<região>:<conta>:alarm:<nome>
    partes = str(alarme.get("AlarmArn", "")).split(":")
    return partes[3] if len(partes) > 3 and partes[3] else REGION


def cloudwatch_em(regiao):
    if regiao not in _cloudwatch_por_regiao:
        _cloudwatch_por_regiao[regiao] = boto3.client("cloudwatch", region_name=regiao)
    return _cloudwatch_por_regiao[regiao]


def link_logs():
    # o console usa "$25" no lugar de "%" dentro do fragmento
    nome = urllib.parse.quote(urllib.parse.quote(COLETOR_LOG_GROUP, safe=""), safe="").replace("%", "$")
    return link_console(f"logsV2:log-groups/log-group/{nome}")


# --- Contexto a partir dos logs e métricas do coletor ------------------------

def eventos_do_coletor(inicio, fim):
    padrao = ('{ ($.level = "ERROR") || ($.coleta_status = "success") || ($.coleta_status = "partial") '
              '|| ($.coleta_status = "skipped") || ($.type = "platform.report") }')
    params = {
        "logGroupName": COLETOR_LOG_GROUP,
        "startTime": int(inicio.timestamp() * 1000),
        "endTime": int(fim.timestamp() * 1000),
        "filterPattern": padrao,
    }
    eventos = []
    for _ in range(10):  # teto de páginas: a janela é curta
        resposta = logs.filter_log_events(**params)
        for evento in resposta.get("events", []):
            try:
                eventos.append(json.loads(evento["message"]))
            except ValueError:
                continue
        if "nextToken" not in resposta:
            break
        params["nextToken"] = resposta["nextToken"]
    return eventos


def resumir(eventos):
    execucoes = Counter()
    erros = Counter()
    tipos = Counter()
    for e in eventos:
        if e.get("coleta_status"):
            execucoes[e["coleta_status"]] += 1
        if e.get("erro_motivo"):
            erros[(e.get("erro_etapa"), e.get("erro_cargo"), e["erro_motivo"])] += 1
            tipos[e.get("erro_tipo") or "inesperado"] += 1
        elif e.get("type") == "platform.report":
            record = e.get("record") or {}
            if record.get("status") == "timeout":
                motivo = "passou do tempo limite da função"
            elif str(record.get("errorType", "")).startswith("Runtime."):
                motivo = record["errorType"]
            else:
                continue  # exceção do coletor: ele mesmo já registrou o motivo
            execucoes["quebrou"] += 1
            erros[("a função quebrou", None, motivo)] += 1
            tipos["quebrou"] += 1
    return execucoes, erros, tipos


def ultima_coleta_completa(ate):
    resposta = cloudwatch.get_metric_data(
        MetricDataQueries=[{
            "Id": "completas",
            "MetricStat": {
                "Metric": {"Namespace": "ColetorTSE", "MetricName": "ColetasCompletas"},
                "Period": 60,
                "Stat": "Sum",
            },
        }],
        StartTime=ate - timedelta(hours=24),
        EndTime=ate,
        ScanBy="TimestampDescending",
    )
    serie = resposta["MetricDataResults"][0]
    for momento, valor in zip(serie["Timestamps"], serie["Values"]):
        if valor > 0:
            return momento
    return None


def inicio_do_problema(nome_alarme, ate, regiao=REGION):
    # quando o alarme entrou em ALARM pela última vez antes de normalizar
    historico = cloudwatch_em(regiao).describe_alarm_history(
        AlarmName=nome_alarme,
        HistoryItemType="StateUpdate",
        StartDate=ate - timedelta(days=2),
        EndDate=ate,
        ScanBy="TimestampDescending",
        MaxRecords=20,
    )
    for item in historico.get("AlarmHistoryItems", []):
        if item["HistorySummary"].endswith("to ALARM"):
            return item["Timestamp"]
    return None


def linhas_de_contexto(momento, janela):
    linhas = []
    dicas = set()

    execucoes, erros, tipos = resumir(eventos_do_coletor(momento - janela, momento))
    dicas.update(tipos)

    total = sum(execucoes.values())
    if total == 0 and not erros:
        dicas.add("nao_rodou")
        linhas.append(f"• O coletor <b>não rodou</b> nos últimos {duracao(janela.total_seconds())}.")
    else:
        partes = [f"{n} {rotulo}" for chave, rotulo in (
            ("success", "completa(s)"), ("partial", "parcial(is)"), ("error", "com falha total"),
            ("skipped", "pulada(s) por lock"), ("quebrou", "quebrada(s)"),
        ) if (n := execucoes.get(chave))]
        linhas.append(f"• Execuções em {duracao(janela.total_seconds())}: {', '.join(partes) or 'nenhuma'}")
        if execucoes.get("skipped", 0) > total / 2:
            dicas.add("lock")
        if execucoes.get("partial"):
            dicas.add("parcial")

    if erros:
        linhas.append("")
        linhas.append("📋 <b>Erros</b>")
        for (etapa, cargo, motivo), n in erros.most_common(MAX_LINHAS_ERRO):
            alvo = cargo or etapa
            linhas.append(f"• {n}× <b>{html.escape(str(alvo))}</b>: {html.escape(motivo[:300])}")
        if len(erros) > MAX_LINHAS_ERRO:
            linhas.append(f"• … e mais {len(erros) - MAX_LINHAS_ERRO} tipo(s) de erro")

    ultima = ultima_coleta_completa(momento)
    linhas.append("")
    if ultima:
        linhas.append(f"🕒 Última coleta completa: <b>{hora(ultima)}</b> "
                      f"(há {duracao((momento - ultima).total_seconds())})")
    else:
        linhas.append("🕒 Nenhuma coleta completa nas últimas 24h")

    dica = next((d for d in PRIORIDADE_DICAS if d in dicas), None)
    if dica:
        linhas.append("")
        linhas.append(f"🔎 <b>O que fazer:</b> {html.escape(DICAS[dica])}")
    return linhas


# --- Mensagem ----------------------------------------------------------------

def formatar(mensagem_sns):
    try:
        alarme = json.loads(mensagem_sns)
    except ValueError:
        # mensagem que não veio de alarme (ex.: teste manual pelo console do SNS)
        return html.escape(mensagem_sns)

    nome = alarme.get("AlarmName", "?")
    estado = alarme.get("NewStateValue", "?")
    regiao = regiao_do_alarme(alarme)
    try:
        momento = parse_data(alarme["StateChangeTime"])
    except (KeyError, ValueError):
        momento = datetime.now(timezone.utc)
    gatilho = alarme.get("Trigger") or {}
    janela = timedelta(seconds=int(gatilho.get("Period", 300)) * int(gatilho.get("EvaluationPeriods", 1)))

    icone, titulo, impacto = ALARMES.get(nome, {}).get(estado) or (
        {"ALARM": "🔴", "OK": "✅"}.get(estado, "⚪"), f"{nome}: {estado}", alarme.get("AlarmDescription") or "",
    )
    linhas = [f"{icone} <b>{html.escape(titulo)}</b>", "",
              html.escape(impacto.format(janela=duracao(janela.total_seconds())))]

    try:
        if estado == "ALARM" and nome in DICAS_CLOUDFRONT:
            linhas += ["", f"🔎 <b>O que fazer:</b> {html.escape(DICAS_CLOUDFRONT[nome])}"]
        elif estado == "ALARM":
            linhas += [""] + linhas_de_contexto(momento, janela)
        elif estado == "OK" and alarme.get("OldStateValue") == "ALARM":
            inicio = inicio_do_problema(nome, momento, regiao)
            if inicio:
                linhas += ["", f"Problema durou <b>{duracao((momento - inicio).total_seconds())}</b> "
                              f"({hora(inicio)} → {hora(momento)})."]
    except Exception as error:
        logger.exception("Não foi possível montar o contexto do alarme")
        linhas += ["", f"<i>(detalhes indisponíveis: {html.escape(type(error).__name__)} — veja os logs)</i>"]

    if nome in DICAS_CLOUDFRONT:
        detalhes = f'<a href="{link_cloudfront()}">Monitoramento do CloudFront</a>'
    else:
        detalhes = f'<a href="{link_logs()}">Logs do coletor</a>'
    linhas += [
        "",
        f'<a href="{link_console("alarmsV2:alarm/" + urllib.parse.quote(nome), regiao)}">Alarme</a> · '
        f'{detalhes} · {hora(momento)} (Brasília)',
    ]
    return "\n".join(linhas)


def chamar_telegram(corpo):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token()}/sendMessage",
        data=json.dumps(corpo).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        resp.read()


def enviar(texto):
    if len(texto) > LIMITE_TELEGRAM:
        texto = texto[:LIMITE_TELEGRAM - 20] + "\n…(cortado)"
    corpo = {"chat_id": CHAT_ID, "text": texto, "parse_mode": "HTML", "disable_web_page_preview": True}

    for tentativa in (1, 2):
        try:
            chamar_telegram(corpo)
            return
        except urllib.error.HTTPError as error:
            # a API do Telegram explica o motivo no corpo (chat_id errado, bot fora do grupo...)
            detalhe = error.read().decode("utf-8", "replace")
            logger.error("Telegram recusou (%s): %s", error.code, detalhe)
            if error.code == 429 and tentativa == 1:
                try:
                    espera = json.loads(detalhe)["parameters"]["retry_after"]
                except (ValueError, KeyError, TypeError):
                    espera = 5
                time.sleep(min(espera, 10))
                continue
            if error.code == 400 and "parse" in detalhe and tentativa == 1:
                # HTML quebrado (ex.: corte no meio de uma tag): manda como texto puro
                corpo = {**corpo, "text": html.unescape(re.sub(r"<[^>]+>", "", texto))}
                corpo.pop("parse_mode")
                continue
            raise


def e_ruido(mensagem_sns):
    # OK vindo de INSUFFICIENT_DATA (alarme recém-criado, ou sem tráfego no
    # CloudFront) não é recuperação de nada: só avisa OK depois de um ALARM
    try:
        alarme = json.loads(mensagem_sns)
    except ValueError:
        return False
    return alarme.get("NewStateValue") == "OK" and alarme.get("OldStateValue") != "ALARM"


def lambda_handler(event, context):
    # erro aqui faz a Lambda retentar (invocação assíncrona do SNS)
    for record in event.get("Records", []):
        if e_ruido(record["Sns"]["Message"]):
            logger.info("Ignorado (OK sem ALARM antes): %s", record["Sns"].get("Subject"))
            continue
        enviar(formatar(record["Sns"]["Message"]))
        logger.info("Alerta enviado ao Telegram: %s", record["Sns"].get("Subject"))
