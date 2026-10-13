import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import boto3
from botocore.exceptions import BotoCoreError, ClientError

# Porte para AWS Lambda do coletor em ../../main.py (Cloud Function).
# Mesma lógica de coleta, delta e caminho espelhado do TSE; troca GCS por S3
# e já incorpora as correções apontadas na análise técnica:
#  - falha de download/gravação de um cargo mantém o item anterior no índice
#  - lock no S3 (escrita condicional) impede execuções sobrepostas
#  - retry com backoff nas chamadas ao TSE (inclusive JSON incompleto),
#    limitado ao tempo que resta da execução — a função termina sozinha
#    (e libera o lock) em vez de estourar o timeout com o TSE lento
#  - cargos baixados em paralelo
#  - falha total (config, coleta vazia, índice) vira erro da função -> alarme
#  - logging com nível (formato JSON da Lambda), filtrável por ERROR/WARNING;
#    erros levam erro_tipo/erro_etapa/erro_cargo/erro_motivo, que o
#    notificador do Telegram lê para explicar o alarme

logger = logging.getLogger()
logger.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

# --- Configuração ---
TSE_BASE_URL = os.environ.get("TSE_BASE_URL", "https://resultados.tse.jus.br")
AMBIENTE = os.environ.get("TSE_AMBIENTE", "oficial")
TURNO = os.environ.get("TSE_TURNO", "1").strip()
# códigos de pleito a coletar (separados por vírgula; vazio = todos). O
# ele-c.json oficial traz também pleitos antigos e eleições suplementares
# (ex.: governador de RR), que não podem entrar na coleta.
PLEITOS = {p.strip() for p in os.environ.get("TSE_PLEITO", "3220").split(",") if p.strip()}
# códigos de eleição a coletar (vazio = todas as do pleito/turno). Trava extra:
# no 2º turno o pleito 3221 também tem suplementares e consultas populares.
ELEICOES = {e.strip() for e in os.environ.get("TSE_ELEICOES", "").split(",") if e.strip()}
UF = os.environ.get("TSE_UF", "ma").lower()

BUCKET_NAME = os.environ["BUCKET_NAME"]
BLOB_NAME = os.environ.get("BLOB_NAME", "dados_eleicao.json")
LOCK_KEY = os.environ.get("LOCK_KEY", "_controle/coletor.lock")
# lock mais velho que isso é considerado abandonado (execução que morreu
# sem liberar) — precisa ser maior que o timeout da função
LOCK_TTL_SEGUNDOS = int(os.environ.get("LOCK_TTL_SEGUNDOS", "180"))
# max-age=0 -> navegador sempre revalida; s-maxage=30 -> CloudFront guarda 30s
CACHE_CONTROL = "public, max-age=0, s-maxage=30, stale-while-revalidate=60"

# ele-c.json quase nunca muda: reaproveita por esse tempo (memória da Lambda
# quente ou cópia no S3) em vez de baixar a cada execução
CONFIG_TTL_SEGUNDOS = int(os.environ.get("CONFIG_TTL_SEGUNDOS", "3600"))
CONFIG_CACHE_KEY = f"_controle/config-{AMBIENTE}.json"

HTTP_TIMEOUT = int(os.environ.get("HTTP_TIMEOUT", "15"))
HTTP_TENTATIVAS = int(os.environ.get("HTTP_TENTATIVAS", "3"))
# não começa uma requisição com menos que isso de prazo (não daria tempo)
HTTP_TIMEOUT_MINIMO = float(os.environ.get("HTTP_TIMEOUT_MINIMO", "2"))
# tempo reservado no fim da execução para gravar no S3 e liberar o lock
MARGEM_SEGUNDOS = float(os.environ.get("MARGEM_SEGUNDOS", "10"))
DOWNLOADS_PARALELOS = int(os.environ.get("DOWNLOADS_PARALELOS", "5"))

CARGOS_FEDERAIS = {"1"}  # presidente -> pasta "br"
CARGOS_ESTADUAIS = {"3", "5", "6", "7"}  # governador, senador, dep. federal, dep. estadual -> pasta = TSE_UF

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/122.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
}

s3 = boto3.client("s3")


class PrazoEsgotado(TimeoutError):
    # sem tempo para (mais) uma requisição antes do timeout da função
    pass


class ColetaFalhou(Exception):
    def __init__(self, etapa, tipo, motivo):
        super().__init__(f"{etapa}: {motivo}")
        self.etapa = etapa
        self.tipo = tipo
        self.motivo = motivo


def descrever_erro(error):
    # (tipo, motivo legível). O tipo agrupa a causa para o notificador sugerir
    # o que fazer: tse_indisponivel, tse_nao_encontrado, tse_conteudo_invalido,
    # s3 ou inesperado.
    if isinstance(error, urllib.error.HTTPError):
        if error.code == 404:
            return "tse_nao_encontrado", "TSE respondeu 404 (arquivo não publicado ou caminho errado)"
        if error.code == 429:
            return "tse_indisponivel", "TSE limitou as requisições (HTTP 429)"
        if error.code >= 500:
            return "tse_indisponivel", f"TSE instável (HTTP {error.code})"
        return "tse_nao_encontrado", f"TSE recusou a requisição (HTTP {error.code})"
    if isinstance(error, PrazoEsgotado):
        return "tse_indisponivel", "TSE lento demais: acabou o tempo da execução antes de concluir o download"
    if isinstance(error, TimeoutError) or (
        isinstance(error, urllib.error.URLError) and isinstance(error.reason, TimeoutError)
    ):
        return "tse_indisponivel", f"TSE não respondeu em {HTTP_TIMEOUT}s"
    if isinstance(error, urllib.error.URLError):
        return "tse_indisponivel", f"falha de rede ao acessar o TSE ({error.reason})"
    if isinstance(error, ConnectionError):
        return "tse_indisponivel", f"conexão com o TSE caiu ({error})"
    if isinstance(error, ValueError):  # JSONDecodeError e UnicodeDecodeError
        return "tse_conteudo_invalido", "TSE devolveu um arquivo que não é JSON válido (incompleto?)"
    if isinstance(error, (KeyError, IndexError, TypeError, AttributeError)):
        return "tse_conteudo_invalido", f"JSON do TSE em formato inesperado ({type(error).__name__}: {error})"
    if isinstance(error, ClientError):
        erro = error.response.get("Error", {})
        return "s3", f"S3 recusou a operação ({erro.get('Code')}: {erro.get('Message')})"
    if isinstance(error, BotoCoreError):
        return "s3", f"falha ao falar com o S3 ({error})"
    return "inesperado", f"{type(error).__name__}: {error}"


def registrar_erro(etapa, tipo, motivo, cargo=None, url=None, fatal=False):
    extra = {"erro_etapa": etapa, "erro_tipo": tipo, "erro_motivo": motivo}
    if fatal:
        # execução inteira falhou: conta junto com success/partial/skipped
        extra["coleta_status"] = "error"
    if cargo:
        extra["erro_cargo"] = cargo
    if url:
        extra["erro_url"] = url
    logger.error("%s%s: %s", etapa, f" [{cargo}]" if cargo else "", motivo, extra=extra)


def baixar_json(url, prazo=None):
    # Retorna (dados_parseados, conteudo_bruto). Tenta de novo em timeout,
    # erro de rede, HTTP 429/5xx e JSON incompleto (arquivo lido no meio da
    # publicação); 404 e afins falham na hora (não adianta). `prazo` é o
    # instante (time.monotonic) em que é preciso parar: o timeout de cada
    # tentativa encolhe para caber nele e não há nova tentativa sem tempo.
    ultimo_erro = None
    for tentativa in range(1, HTTP_TENTATIVAS + 1):
        timeout = HTTP_TIMEOUT
        if prazo is not None:
            restante = prazo - time.monotonic()
            if restante < HTTP_TIMEOUT_MINIMO:
                raise PrazoEsgotado(f"sem tempo para baixar {url}") from ultimo_erro
            timeout = min(HTTP_TIMEOUT, restante)
        try:
            request = urllib.request.Request(url, headers=HEADERS)
            with urllib.request.urlopen(request, timeout=timeout) as response:
                content = response.read()
            return json.loads(content.decode("utf-8")), content
        except urllib.error.HTTPError as error:
            if error.code != 429 and error.code < 500:
                raise
            ultimo_erro = error
        except (urllib.error.URLError, TimeoutError, ConnectionError, ValueError) as error:
            ultimo_erro = error

        if tentativa < HTTP_TENTATIVAS:
            espera = 2 ** (tentativa - 1)  # 1s, 2s, ...
            if prazo is not None and prazo - time.monotonic() - espera < HTTP_TIMEOUT_MINIMO:
                break  # não daria tempo: falha com o erro real, não com PrazoEsgotado
            logger.warning("Tentativa %d/%d falhou para %s: %s — nova tentativa em %ds",
                           tentativa, HTTP_TENTATIVAS, url, descrever_erro(ultimo_erro)[1], espera)
            time.sleep(espera)

    raise ultimo_erro


def chave_item(item):
    return f'{item["pleito"]}:{item["eleicao"]}:{item["cargo"]}:{item.get("abrangencia", "br")}'


def extrair_timestamp_geracao(dados):
    # TSE marca a geração do arquivo em dt/ht ou dg/hg, dependendo do arquivo.
    if not isinstance(dados, dict):
        return None
    for chave_data, chave_hora in (("dg", "hg"), ("dt", "ht")):
        if dados.get(chave_data) and dados.get(chave_hora):
            return f'{dados[chave_data]} {dados[chave_hora]}'
    return None


def adquirir_lock(dono, chave=LOCK_KEY, ttl=LOCK_TTL_SEGUNDOS):
    # Cria o lock só se ele não existir (If-None-Match: *). Se existir mas
    # estiver velho demais, assume o lock sobrescrevendo com If-Match no ETag
    # lido — se outra execução fizer o mesmo ao mesmo tempo, só uma vence.
    # chave/ttl: o feed usa locks próprios, para não travar o espelho.
    corpo = json.dumps({"dono": dono, "criado_em": agora_iso()})
    try:
        s3.put_object(Bucket=BUCKET_NAME, Key=chave, Body=corpo, IfNoneMatch="*")
        return True
    except ClientError as error:
        if error.response["Error"]["Code"] not in ("PreconditionFailed", "ConditionalRequestConflict"):
            raise

    try:
        atual = s3.head_object(Bucket=BUCKET_NAME, Key=chave)
    except ClientError:
        return False  # lock sumiu entre as chamadas; a próxima execução pega

    idade = (datetime.now(timezone.utc) - atual["LastModified"]).total_seconds()
    if idade < ttl:
        return False

    logger.warning("Lock abandonado há %ds, assumindo", int(idade))
    try:
        s3.put_object(Bucket=BUCKET_NAME, Key=chave, Body=corpo, IfMatch=atual["ETag"])
        return True
    except ClientError:
        return False


def liberar_lock(chave=LOCK_KEY, ttl=LOCK_TTL_SEGUNDOS):
    try:
        s3.delete_object(Bucket=BUCKET_NAME, Key=chave)
    except (ClientError, BotoCoreError) as error:
        tipo, motivo = descrever_erro(error)
        registrar_erro("liberação do lock", tipo,
                       f"{motivo} — próximas execuções podem ser puladas por até {ttl}s")


def carregar_estado_anterior():
    # Lê o último índice salvo no bucket (se existir) pra detecção de mudança (delta).
    try:
        resposta = s3.get_object(Bucket=BUCKET_NAME, Key=BLOB_NAME)
        payload_anterior = json.loads(resposta["Body"].read())
    except ClientError as error:
        if error.response["Error"]["Code"] == "NoSuchKey":
            return {}
        raise ColetaFalhou("leitura do índice anterior", *descrever_erro(error)) from error
    except ValueError as error:
        logger.warning("Índice anterior ilegível, tratando como primeira execução: %s", error)
        return {}

    return {chave_item(item): item for item in payload_anterior.get("resultados", [])}


def caminho_espelhado_tse(ciclo, codigo_eleicao, uf_pasta, arquivo):
    # Mesmo caminho relativo do arquivo no TSE ({ambiente}/{ciclo}/{codEleicao}/dados/{uf}/{arquivo}),
    # igual o `divulga` replicava pro SFTP remoto trocando só o host.
    return f"{AMBIENTE}/{ciclo}/{codigo_eleicao}/dados/{uf_pasta}/{arquivo}"


def salvar_json(chave, conteudo):
    s3.put_object(
        Bucket=BUCKET_NAME,
        Key=chave,
        Body=conteudo,
        ContentType="application/json",
        CacheControl=CACHE_CONTROL,
    )
    logger.info("Salvo s3://%s/%s", BUCKET_NAME, chave)


def agora_iso():
    return datetime.now(timezone.utc).isoformat()


# (config, obtida_em) — sobrevive entre execuções no mesmo ambiente quente
_config_cache = None


def ler_config_salva(config_url):
    # Cópia do ele-c.json no S3: (config, obtida_em) ou None. A origem gravada
    # nos metadados evita reaproveitar a config de outra TseBaseUrl.
    try:
        resposta = s3.get_object(Bucket=BUCKET_NAME, Key=CONFIG_CACHE_KEY)
        if resposta.get("Metadata", {}).get("origem") != config_url:
            return None
        return json.loads(resposta["Body"].read()), resposta["LastModified"].timestamp()
    except ClientError as error:
        if error.response["Error"]["Code"] != "NoSuchKey":
            logger.warning("Não foi possível ler a cópia da configuração no S3: %s", descrever_erro(error)[1])
        return None
    except (BotoCoreError, ValueError) as error:
        logger.warning("Cópia da configuração no S3 inutilizável: %s", error)
        return None


def obter_config(config_url, forcar=False, prazo=None):
    # Retorna (config, fonte). Ordem: memória -> cópia no S3 -> TSE. Se o TSE
    # falhar, usa a cópia que houver, mesmo vencida — melhor que parar a coleta.
    global _config_cache
    agora = time.time()
    salva = None
    if not forcar:
        if _config_cache and agora - _config_cache[1] < CONFIG_TTL_SEGUNDOS:
            return _config_cache[0], "memória"
        salva = ler_config_salva(config_url)
        if salva and agora - salva[1] < CONFIG_TTL_SEGUNDOS:
            _config_cache = salva
            return salva[0], "cópia no S3"

    try:
        config, conteudo_bruto = baixar_json(config_url, prazo)
    except Exception as error:
        reserva = _config_cache or salva or ler_config_salva(config_url)
        if not reserva:
            raise
        logger.warning("TSE indisponível para o ele-c.json (%s) — usando cópia de %d min atrás",
                       descrever_erro(error)[1], (agora - reserva[1]) // 60)
        return reserva[0], "cópia vencida (TSE indisponível)"

    _config_cache = (config, agora)
    try:
        s3.put_object(Bucket=BUCKET_NAME, Key=CONFIG_CACHE_KEY, Body=conteudo_bruto,
                      ContentType="application/json", Metadata={"origem": config_url})
    except (ClientError, BotoCoreError) as error:
        logger.warning("Não foi possível salvar a cópia da configuração no S3: %s", descrever_erro(error)[1])
    return config, "TSE"


def listar_alvos(config):
    # Gera (pleito, eleicao, cargo, uf_pasta, abrangencia) para cada arquivo
    # a coletar, filtrando pelo pleito e turno configurados.
    for pleito in config.get("pl", []):
        if PLEITOS and str(pleito["cd"]) not in PLEITOS:
            continue
        for eleicao in pleito.get("e", []):
            # str(): o TSE pode mandar o turno como número ou texto
            if str(eleicao.get("t", TURNO)) != TURNO:
                continue
            if ELEICOES and str(eleicao["cd"]) not in ELEICOES:
                continue

            # nas eleições gerais o índice traz abr.cd = "br" e a pasta/UF é
            # decidida pelo código do cargo; abrangência de outra UF (eleição
            # suplementar, consulta popular) não é nossa — a não ser que a
            # eleição esteja em TSE_ELEICOES (ex.: governador do 2º turno, 6260)
            explicita = str(eleicao["cd"]) in ELEICOES
            for abrangencia in eleicao.get("abr", []):
                cod_uf = str(abrangencia.get("cd", "br")).lower()
                if cod_uf in ("br", UF):
                    uf_pasta, rotulo_estadual = UF, "estadual"
                elif explicita:
                    uf_pasta, rotulo_estadual = cod_uf, f"estadual-{cod_uf}"
                else:
                    continue
                for cargo in abrangencia.get("cp", []):
                    if str(cargo["cd"]) in CARGOS_FEDERAIS:
                        yield pleito, eleicao, cargo, "br", "federal"
                    elif str(cargo["cd"]) in CARGOS_ESTADUAIS:
                        yield pleito, eleicao, cargo, uf_pasta, rotulo_estadual
                    # demais: cargo municipal ou exclusivo do DF - ainda não coberto


def preparar_alvo(pleito, eleicao, cargo, uf_pasta, cod_abrangencia):
    # (item do índice, url no TSE, rótulo para logs/alertas)
    ciclo = pleito["c"]  # → "ele2026"; cada pleito tem o seu
    codigo_eleicao = str(eleicao["cd"])
    codigo_cargo = str(cargo["cd"])
    arquivo = f"{uf_pasta}-c{codigo_cargo.zfill(4)}-e{codigo_eleicao.zfill(6)}-u.json"
    url = f"{TSE_BASE_URL}/{AMBIENTE}/{ciclo}/{codigo_eleicao}/dados/{uf_pasta}/{arquivo}"
    item = {
        "pleito": str(pleito["cd"]),
        "eleicao": codigo_eleicao,
        "nome_eleicao": eleicao.get("nm", ""),
        "cargo": codigo_cargo,
        "nome_cargo": cargo.get("ds", ""),
        "abrangencia": cod_abrangencia,
        "arquivo": caminho_espelhado_tse(ciclo, codigo_eleicao, uf_pasta, arquivo),
    }
    rotulo = f'{item["nome_cargo"] or "cargo " + item["cargo"]} ({uf_pasta.upper()})'
    return item, url, rotulo


def coletar(prazo=None):
    estado_anterior = carregar_estado_anterior()

    # 1. Configuração geral — sem ela não há o que coletar
    config_url = f"{TSE_BASE_URL}/{AMBIENTE}/comum/config/ele-c.json"
    try:
        config, fonte = obter_config(config_url, prazo=prazo)
        alvos = list(listar_alvos(config))
        if not alvos and fonte != "TSE":
            # cópia antiga pode não ter o turno novo (ex.: troca para o 2º turno)
            logger.info("Configuração em cache sem eleição do turno %s — buscando no TSE", TURNO)
            config, fonte = obter_config(config_url, forcar=True, prazo=prazo)
            alvos = list(listar_alvos(config))
        ciclos = sorted({pleito["c"] for pleito, *_ in alvos})
    except Exception as error:
        tipo, motivo = descrever_erro(error)
        raise ColetaFalhou("configuração do TSE (ele-c.json)", tipo, f"{motivo} — {config_url}") from error

    logger.info("Configuração (%s) | pleito %s | ciclo %s",
                fonte, ",".join(sorted(PLEITOS)) or "todos", ",".join(ciclos))
    if not alvos:
        raise ColetaFalhou(
            "configuração do TSE (ele-c.json)", "tse_nao_encontrado",
            f"nenhuma eleição do pleito {','.join(sorted(PLEITOS)) or '(todos)'} / turno {TURNO}"
            f"{' / eleições ' + ','.join(sorted(ELEICOES)) if ELEICOES else ''} "
            "com os cargos cobertos — confira TsePleito/TseTurno/TseEleicoes/TseAmbiente",
        )

    resultados_finais = []
    houve_mudanca = False
    cargos_com_erro = []
    tipos_de_erro = Counter()

    # 2. Baixa os cargos cobertos em paralelo (só a parte HTTP; S3 e índice
    # seguem em série, na ordem do ele-c.json)
    preparados = [preparar_alvo(*alvo) for alvo in alvos]
    for item, url, _ in preparados:
        logger.info("%s / %s (%s): %s", item["nome_eleicao"], item["nome_cargo"], item["abrangencia"], url)
    with ThreadPoolExecutor(max_workers=max(1, min(DOWNLOADS_PARALELOS, len(preparados)))) as pool:
        downloads = [pool.submit(baixar_json, url, prazo) for _, url, _ in preparados]

    for (item, url, rotulo), download in zip(preparados, downloads):
        item_anterior = estado_anterior.get(chave_item(item))

        try:
            dados, conteudo_bruto = download.result()
        except Exception as error:
            tipo, motivo = descrever_erro(error)
            registrar_erro("download do cargo", tipo, motivo, cargo=rotulo, url=url)
            cargos_com_erro.append(rotulo)
            tipos_de_erro[tipo] += 1
            # mantém o que já estava publicado em vez de sumir com o cargo do índice
            if item_anterior:
                resultados_finais.append(item_anterior)
            continue

        item["gerado_em"] = extrair_timestamp_geracao(dados)

        # 3. Delta: compara a geração do arquivo com a do último índice salvo.
        # Sem timestamp identificável assume mudança, por segurança.
        timestamp_anterior = item_anterior.get("gerado_em") if item_anterior else None
        mudou = (
            item_anterior is None
            or item["gerado_em"] is None
            or item["gerado_em"] != timestamp_anterior
        )

        if mudou:
            logger.info("Atualizado (%r -> %r)", timestamp_anterior, item["gerado_em"])
            # conteúdo exatamente como veio do TSE, no caminho espelhado
            try:
                salvar_json(item["arquivo"], conteudo_bruto)
            except (ClientError, BotoCoreError) as error:
                tipo, motivo = descrever_erro(error)
                registrar_erro("gravação do cargo no S3", tipo, motivo, cargo=rotulo)
                cargos_com_erro.append(rotulo)
                tipos_de_erro[tipo] += 1
                if item_anterior:
                    resultados_finais.append(item_anterior)
                continue
            houve_mudanca = True
        else:
            logger.info("Sem mudança (geração: %s)", item["gerado_em"])

        resultados_finais.append(item)

    # 4. Índice leve: só regrava se algo mudou (ou na primeira execução)
    if not resultados_finais:
        # herda a causa mais comum entre os cargos (ex.: TSE fora -> tse_indisponivel)
        tipo = tipos_de_erro.most_common(1)[0][0] if tipos_de_erro else "inesperado"
        raise ColetaFalhou(
            "coleta dos cargos", tipo,
            f"nenhum cargo foi coletado — todos os {len(alvos)} falharam (ver erros acima)",
        )

    if houve_mudanca or not estado_anterior:
        payload = {
            "atualizado_em": agora_iso(),
            "total": len(resultados_finais),
            "resultados": resultados_finais,
        }
        try:
            salvar_json(BLOB_NAME, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
        except (ClientError, BotoCoreError) as error:
            raise ColetaFalhou("gravação do índice no S3", *descrever_erro(error)) from error
    else:
        logger.info("Nenhum cargo mudou desde a última coleta — índice não foi reescrito.")

    return {
        "status": "partial" if cargos_com_erro else "success",
        "total": len(resultados_finais),
        "falhas": len(cargos_com_erro),
        "cargos_com_erro": cargos_com_erro,
        "mudou": houve_mudanca,
        "atualizado_em": agora_iso(),
    }


def lambda_handler(event, context):
    logger.info("Iniciando coletor TSE | Ambiente: %s | Pleito: %s | Turno: %s | UF: %s",
                AMBIENTE, ",".join(sorted(PLEITOS)) or "todos", TURNO, UF.upper())

    dono = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    # instante-limite para parar de falar com o TSE, deixando a margem para
    # gravar no S3 e liberar o lock antes do timeout da função
    restante_ms = getattr(context, "get_remaining_time_in_millis", None)
    prazo = time.monotonic() + restante_ms() / 1000 - MARGEM_SEGUNDOS if restante_ms else None
    try:
        conseguiu_lock = adquirir_lock(dono)
    except (ClientError, BotoCoreError) as error:
        registrar_erro("lock no S3", *descrever_erro(error), fatal=True)
        raise

    if not conseguiu_lock:
        logger.warning("Outra coleta em andamento — execução ignorada.", extra={"coleta_status": "skipped"})
        return {"status": "skipped", "motivo": "coleta em andamento"}

    try:
        resultado = coletar(prazo)
    except ColetaFalhou as error:
        registrar_erro(error.etapa, error.tipo, error.motivo, fatal=True)
        raise
    except Exception as error:
        registrar_erro("erro inesperado no coletor", *descrever_erro(error), fatal=True)
        raise
    finally:
        liberar_lock()

    # falha parcial não derruba a execução, mas fica visível como WARNING
    if resultado["falhas"]:
        logger.warning("Coleta parcial: %d cargo(s) com erro: %s",
                       resultado["falhas"], ", ".join(resultado["cargos_com_erro"]))
    # coleta_status vira campo próprio no JSON do log -> métrica do alarme de dados parados
    logger.info("Coleta finalizada: %s", json.dumps(resultado, ensure_ascii=False),
                extra={"coleta_status": resultado["status"]})
    return resultado
