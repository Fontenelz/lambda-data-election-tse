import gzip
import http.client
import io
import json
import logging
import os
import resource
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import namedtuple
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from botocore.exceptions import BotoCoreError, ClientError

import contrato
import index as coletor

# Feed da página /eleicoes/ao-vivo do imirante.com (contrato v1), gravado no
# mesmo bucket sob FEED_PREFIXO e servido pelo mesmo CloudFront. Roda em
# duas funções próprias, cada uma com seu lock — o espelho (index.py) não
# depende delas e vice-versa:
#  - handler_rapido (coletor-tse-feed): a cada ~15s, Presidente no Brasil,
#    nas 27 UFs + exterior e nos municípios de TSE_UF -> agora.json,
#    municipios-<uf>.json e historico.json, nessa ordem (agora.json por
#    último: quem vê um seq novo encontra os outros dois já gravados)
#  - handler_municipios (coletor-tse-feed-municipios): a cada ~1 min, os
#    municípios do Brasil -> municipios-br.json (colunar)
# Reaproveita do coletor a configuração (ele-c.json em cache), filtros de
# pleito/turno/eleição, lock no S3 e classificação de erros (erro_*).
# Nada é gravado sem passar pelo contrato (contrato.py); arquivo do TSE que
# falha usa o último valor bom, nunca zero.

logger = logging.getLogger()

FEED_PREFIXO = os.environ.get("FEED_PREFIXO", "aovivo/2026/t2/").strip("/") + "/"
FEED_BASE_URL = os.environ.get("FEED_BASE_URL", "https://d3nthtia8j7edn.cloudfront.net").rstrip("/")
FEED_RECARREGAR = os.environ.get("FEED_RECARREGAR", "").strip() or None
FEED_AVISO = os.environ.get("FEED_AVISO", "").strip() or None
UF = coletor.UF

FEED_LOCK_KEY = os.environ.get("FEED_LOCK_KEY", "_controle/feed.lock")
FEED_LOCK_TTL = int(os.environ.get("FEED_LOCK_TTL_SEGUNDOS", "90"))
FEED_MUN_LOCK_KEY = os.environ.get("FEED_MUN_LOCK_KEY", "_controle/feed-municipios.lock")
FEED_MUN_LOCK_TTL = int(os.environ.get("FEED_MUN_LOCK_TTL_SEGUNDOS", "180"))

# ciclo rápido: vários ciclos por invocação (a cada INTERVALO, até JANELA)
FEED_INTERVALO = float(os.environ.get("FEED_INTERVALO_SEGUNDOS", "15"))
FEED_JANELA = float(os.environ.get("FEED_JANELA_SEGUNDOS", "45"))
# reservado no fim da execução para gravar no S3 e liberar o lock
FEED_MARGEM = float(os.environ.get("FEED_MARGEM_SEGUNDOS", "5"))
FEED_THREADS = int(os.environ.get("FEED_THREADS", "16"))
FEED_MUN_THREADS = int(os.environ.get("FEED_MUN_THREADS", "8"))
# teto de requisições/s da varredura dos municípios: com 32 threads sem teto o
# TSE respondeu 429 (teste na AWS, 2026-10-13); o que não couber fica para a
# próxima execução
FEED_MUN_REQ_POR_SEGUNDO = float(os.environ.get("FEED_MUN_REQ_POR_SEGUNDO", "25"))
# 429 sem Retry-After: pausa todas as threads por isso (teto de 30s)
FEED_PAUSA_429 = float(os.environ.get("FEED_PAUSA_429_SEGUNDOS", "5"))
FEED_HTTP_TIMEOUT = float(os.environ.get("FEED_HTTP_TIMEOUT", "5"))
FEED_HTTP_TENTATIVAS = int(os.environ.get("FEED_HTTP_TENTATIVAS", "3"))
FEED_HISTORICO_MAX = int(os.environ.get("FEED_HISTORICO_MAX", "600"))

CONTENT_TYPE = "application/json; charset=utf-8"
CACHE_RAPIDO = "public, max-age=5, s-maxage=5, stale-while-revalidate=30, stale-if-error=600"
CACHE_MUNICIPIOS = "public, max-age=10, s-maxage=5, stale-while-revalidate=30, stale-if-error=600"
# foto de candidato não muda durante a apuração
CACHE_FOTO = "public, max-age=86400, s-maxage=86400"

ARQ_AGORA = "agora.json"
ARQ_HISTORICO = "historico.json"
ARQ_MUN_UF = f"municipios-{UF}.json"
ARQ_MUN_BR = "municipios-br.json"

# Brasil não tem mais horário de verão
BRASILIA = timezone(timedelta(hours=-3))

with open(Path(__file__).resolve().parent / "municipios_ibge.json", encoding="utf-8") as _arquivo:
    NOMES_IBGE = json.load(_arquivo)


def _ler_cores():
    try:
        cores = json.loads(os.environ.get("FEED_CORES", "").strip() or "{}")
        if isinstance(cores, dict):
            return {str(n): str(c) for n, c in cores.items()}
    except ValueError:
        pass
    logger.warning("FEED_CORES inválido (esperado JSON como {\"13\": \"#C0392B\"}) — cores ignoradas")
    return {}


FEED_CORES = _ler_cores()


class FeedFalhou(Exception):
    def __init__(self, etapa, tipo, motivo, alvo=None):
        super().__init__(f"{etapa}: {motivo}")
        self.etapa = etapa
        self.tipo = tipo
        self.motivo = motivo
        self.alvo = alvo


def registrar_falha(etapa, tipo, motivo, alvo=None):
    # mesmo padrão do coletor (erro_*), que o notificador lê; feed_status
    # separa do coleta_status do espelho
    extra = {"erro_etapa": etapa, "erro_tipo": tipo, "erro_motivo": motivo, "feed_status": "erro"}
    if alvo:
        extra["erro_cargo"] = alvo
    logger.error("%s%s: %s", etapa, f" [{alvo}]" if alvo else "", motivo, extra=extra)


def agora_brasilia():
    return datetime.now(BRASILIA).replace(microsecond=0)


# --- TSE: HTTP com conexão persistente, gzip e GET condicional ---------------

Resposta = namedtuple("Resposta", "dados etag modificado_em modificado")


class ClienteTSE:
    # Uma conexão HTTP(S) por thread, reaproveitada entre requisições (o ciclo
    # lento faz milhares; abrir TLS a cada uma pesaria na CPU da Lambda).
    # Manda If-None-Match/If-Modified-Since com o que tiver do arquivo: 304
    # devolve o último conteúdo bom guardado (guardar=True) ou só avisa que
    # não mudou (guardar=False, quem chama guarda os validadores).
    # por_segundo: teto de requisições/s somando todas as threads. HTTP 429
    # não é repetido: pausa todas as threads pelo Retry-After e falha o arquivo
    # (último valor bom); insistir só prolonga o bloqueio do TSE.

    def __init__(self, timeout=FEED_HTTP_TIMEOUT, tentativas=FEED_HTTP_TENTATIVAS, por_segundo=None):
        self.timeout = timeout
        self.tentativas = tentativas
        self.intervalo = 1 / por_segundo if por_segundo else 0.0
        self.cache = {}  # url -> Resposta (último conteúdo bom)
        self._local = threading.local()
        self._trava = threading.Lock()
        self._proxima_vez = 0.0  # time.monotonic() da próxima requisição liberada
        self._pausa_ate = 0.0
        self.zerar_contadores()

    def zerar_contadores(self):
        self.requisicoes = 0
        self.nao_modificados = 0
        self.limitadas = 0  # respostas 429

    def _esperar_vez(self, url, prazo):
        with self._trava:
            vez = max(time.monotonic(), self._proxima_vez, self._pausa_ate)
            if prazo is not None and vez > prazo - 1:
                raise coletor.PrazoEsgotado(f"sem tempo para baixar {url}")
            self._proxima_vez = vez + self.intervalo
        espera = vez - time.monotonic()
        if espera > 0:
            time.sleep(espera)

    def _pausar(self, cabecalhos_resposta):
        try:
            segundos = float(cabecalhos_resposta.get("Retry-After") or FEED_PAUSA_429)
        except ValueError:  # Retry-After em formato de data
            segundos = FEED_PAUSA_429
        with self._trava:
            self.limitadas += 1
            self._pausa_ate = max(self._pausa_ate, time.monotonic() + min(max(segundos, 1), 30))

    def _contar(self, nao_modificado):
        with self._trava:
            self.requisicoes += 1
            self.nao_modificados += int(nao_modificado)

    def _get(self, url, cabecalhos, timeout):
        partes = urllib.parse.urlsplit(url)
        conexoes = self._local.__dict__.setdefault("conexoes", {})
        chave = (partes.scheme, partes.netloc)
        conexao = conexoes.get(chave)
        if conexao is None:
            classe = http.client.HTTPSConnection if partes.scheme == "https" else http.client.HTTPConnection
            conexao = conexoes[chave] = classe(partes.netloc, timeout=timeout)
        conexao.timeout = timeout
        if conexao.sock is not None:
            conexao.sock.settimeout(timeout)
        caminho = partes.path + (f"?{partes.query}" if partes.query else "")
        try:
            conexao.request("GET", caminho, headers=cabecalhos)
            resposta = conexao.getresponse()
            corpo = resposta.read()
        except BaseException:
            conexao.close()
            conexoes.pop(chave, None)
            raise
        if resposta.will_close:
            conexao.close()
            conexoes.pop(chave, None)
        return resposta.status, resposta.reason, resposta.headers, corpo

    def baixar(self, url, prazo=None, guardar=True, etag=None, modificado_em=None):
        anterior = self.cache.get(url) if guardar else None
        if anterior:
            etag, modificado_em = anterior.etag, anterior.modificado_em
        cabecalhos = dict(coletor.HEADERS, **{"Accept-Encoding": "gzip"})
        if etag:
            cabecalhos["If-None-Match"] = etag
        if modificado_em:
            cabecalhos["If-Modified-Since"] = modificado_em

        ultimo_erro = None
        for tentativa in range(1, self.tentativas + 1):
            self._esperar_vez(url, prazo)
            timeout = self.timeout
            if prazo is not None:
                restante = prazo - time.monotonic()
                if restante < 1:
                    raise coletor.PrazoEsgotado(f"sem tempo para baixar {url}") from ultimo_erro
                timeout = min(timeout, restante)
            try:
                status, motivo, cabecalhos_resposta, corpo = self._get(url, cabecalhos, timeout)
            except TimeoutError as erro:
                ultimo_erro = erro
                continue
            except (OSError, http.client.HTTPException) as erro:
                # conexão reaproveitada que o TSE fechou, DNS, resposta truncada:
                # tudo vira "conexão caiu" para o descrever_erro do coletor
                ultimo_erro = ConnectionError(f"{type(erro).__name__}: {erro}")
                continue

            if status == 304 and (anterior or etag or modificado_em):
                self._contar(True)
                return anterior or Resposta(None, etag, modificado_em, False)
            self._contar(False)
            if status == 200:
                try:
                    if (cabecalhos_resposta.get("Content-Encoding") or "").lower() == "gzip":
                        corpo = gzip.decompress(corpo)
                    dados = json.loads(corpo.decode("utf-8"))
                except (ValueError, OSError, EOFError) as erro:  # JSON/gzip incompleto
                    ultimo_erro = ValueError(f"conteúdo inválido: {erro}")
                    continue
                resposta = Resposta(dados, cabecalhos_resposta.get("ETag"),
                                    cabecalhos_resposta.get("Last-Modified"), True)
                if guardar:
                    self.cache[url] = resposta
                return resposta

            erro = urllib.error.HTTPError(url, status, motivo, cabecalhos_resposta, io.BytesIO(corpo))
            if status == 429:
                self._pausar(cabecalhos_resposta)
                raise erro
            if status < 500:
                raise erro  # 404 e afins: não adianta tentar de novo
            ultimo_erro = erro
            if tentativa < self.tentativas:
                time.sleep(0.2 * tentativa)
        raise ultimo_erro

    def ultimo_bom(self, url):
        resposta = self.cache.get(url)
        return resposta.dados if resposta else None


def baixar_bytes(url, timeout=FEED_HTTP_TIMEOUT * 2):
    request = urllib.request.Request(url, headers=coletor.HEADERS)
    with urllib.request.urlopen(request, timeout=timeout) as resposta:
        return resposta.read()


# --- Destino: S3 (Lambda) ou pasta local (modo CLI) --------------------------

class DestinoS3:
    def ler_json(self, chave):
        try:
            resposta = coletor.s3.get_object(Bucket=coletor.BUCKET_NAME, Key=chave)
        except ClientError as erro:
            if erro.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise
        return json.loads(resposta["Body"].read())

    def existe(self, chave):
        try:
            coletor.s3.head_object(Bucket=coletor.BUCKET_NAME, Key=chave)
            return True
        except ClientError as erro:
            if erro.response["Error"]["Code"] in ("404", "NoSuchKey", "NotFound"):
                return False
            raise

    def gravar(self, chave, corpo, content_type=CONTENT_TYPE, cache_control=None):
        extra = {"CacheControl": cache_control} if cache_control else {}
        coletor.s3.put_object(Bucket=coletor.BUCKET_NAME, Key=chave, Body=corpo, ContentType=content_type, **extra)


class DestinoPasta:
    def __init__(self, raiz):
        self.raiz = Path(raiz)

    def ler_json(self, chave):
        caminho = self.raiz / chave
        return json.loads(caminho.read_text("utf-8")) if caminho.exists() else None

    def existe(self, chave):
        return (self.raiz / chave).exists()

    def gravar(self, chave, corpo, content_type=CONTENT_TYPE, cache_control=None):
        caminho = self.raiz / chave
        caminho.parent.mkdir(parents=True, exist_ok=True)
        caminho.write_bytes(corpo)


def json_bytes(doc):
    return json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


# --- Leitura dos arquivos do TSE ----------------------------------------------

def inteiro(valor):
    # o TSE manda números como texto ("56104503"); vazio = ainda sem dado
    if valor is None or valor == "":
        return 0
    return int(str(valor).strip())


def iso_tse(data, hora):
    # "05/10/2026" + "12:51:05" -> "2026-10-05T12:51:05-03:00"
    return datetime.strptime(f"{data} {hora}", "%d/%m/%Y %H:%M:%S").replace(tzinfo=BRASILIA).isoformat()


def candidatos_tse(dados):
    for cargo in dados.get("carg", []):
        for agremiacao in cargo.get("agr", []):
            for partido in agremiacao.get("par", []):
                for candidato in partido.get("cand", []):
                    yield partido, candidato


def foi_eleito(candidato):
    # e="s" também aparece no 1º turno para quem foi ao 2º (st="2º turno");
    # eleito de fato é st "Eleito" (ou matematicamente eleito)
    situacao = str(candidato.get("st", "")).strip().lower()
    return candidato.get("e") == "s" and (situacao.startswith("eleito") or "matematicamente" in situacao)


def url_foto(sqcand):
    return f"{FEED_BASE_URL}/{FEED_PREFIXO}fotos/{sqcand}.jpeg"


def montar_candidatos(dados_br):
    candidatos = []
    for partido, cand in candidatos_tse(dados_br):
        numero = str(cand["n"])
        vice = next((v.get("nmu") or v.get("nm") for v in cand.get("vs", []) if v.get("tp") == "v"), None)
        candidatos.append((inteiro(cand.get("seq")) or 999, numero, {
            "numero": numero,
            "nome": cand.get("nmu") or cand.get("nm"),
            "partido": partido.get("sg"),
            "vice": vice or None,
            "sqcand": str(cand["sqcand"]),
            "foto": url_foto(cand["sqcand"]),
            "cor": FEED_CORES.get(numero),
        }))
    # ordem do TSE (seq), não a de leitura do arquivo
    return [c for _, _, c in sorted(candidatos, key=lambda item: item[:2])]


def votos_por_numero(dados, numeros):
    votos = dict.fromkeys(numeros, 0)
    for _, cand in candidatos_tse(dados):
        if str(cand["n"]) in votos:
            votos[str(cand["n"])] = inteiro(cand.get("vap"))
    return votos


def situacao(secoes, totalizadas):
    if secoes == 0 or totalizadas == 0:
        return "nao-iniciada"
    return "encerrada" if totalizadas >= secoes else "apurando"


def montar_totais(dados, numeros):
    s, e, v = dados["s"], dados["e"], dados["v"]
    secoes, totalizadas = inteiro(s.get("ts")), inteiro(s.get("st"))
    eleito = next((str(c["n"]) for _, c in candidatos_tse(dados) if foi_eleito(c) and str(c["n"]) in numeros), None)
    return {
        "secoes": secoes,
        "totalizadas": totalizadas,
        "eleitorado": inteiro(e.get("te")),
        "comparecimento": inteiro(e.get("c")),
        "abstencao": inteiro(e.get("a")),
        "validos": inteiro(v.get("vv")),
        "brancos": inteiro(v.get("vb")),
        "nulos": inteiro(v.get("tvn")),  # inclui nulos técnicos (vnt)
        "votos": votos_por_numero(dados, numeros),
        "situacao": situacao(secoes, totalizadas),
        "eleito": eleito,
    }


def montar_municipio(dados, numeros):
    s, e, v = dados["s"], dados["e"], dados["v"]
    return {
        "secoes": inteiro(s.get("ts")),
        "totalizadas": inteiro(s.get("st")),
        "eleitorado": inteiro(e.get("te")),
        "comparecimento": inteiro(e.get("c")),
        "brancos": inteiro(v.get("vb")),
        "nulos": inteiro(v.get("tvn")),
        "votos": votos_por_numero(dados, numeros),
    }


def municipio_zerado(numeros):
    # nunca baixado (nem antes da falha): a página exige o município presente
    return {"secoes": 0, "totalizadas": 0, "eleitorado": 0, "comparecimento": 0,
            "brancos": 0, "nulos": 0, "votos": dict.fromkeys(numeros, 0)}


def fracao(totais):
    return totais["totalizadas"] / totais["secoes"] if totais["secoes"] else 0.0


def atualizar_historico(anterior, seq, momento, br, uf):
    # novo ponto só quando a fração totalizada do Brasil ou da UF muda; até
    # FEED_HISTORICO_MAX pontos, descartando os mais antigos menos o primeiro
    pontos = list((anterior or {}).get("pontos", []))
    ponto = {"t": momento,
             "br": {"pst": fracao(br), "votos": br["votos"]},
             UF: {"pst": fracao(uf), "votos": uf["votos"]}}
    if pontos and set(pontos[-1]["br"]["votos"]) != set(br["votos"]):
        logger.warning("Histórico anterior é de outros candidatos (%s) — recomeçando",
                       ",".join(sorted(pontos[-1]["br"]["votos"])))
        pontos = []
    ultimo = pontos[-1] if pontos else None
    if not ultimo or ultimo["br"]["pst"] != ponto["br"]["pst"] or ultimo[UF]["pst"] != ponto[UF]["pst"]:
        pontos.append(ponto)
    if len(pontos) > FEED_HISTORICO_MAX:
        pontos = pontos[:1] + pontos[-(FEED_HISTORICO_MAX - 1):]
    return {"versao": 1, "seq": seq, "pontos": pontos}


# --- Configuração -------------------------------------------------------------

def config_via_coletor(prazo=None):
    # ele-c.json com o cache do coletor (memória -> cópia no S3 -> TSE)
    url = f"{coletor.TSE_BASE_URL}/{coletor.AMBIENTE}/comum/config/ele-c.json"
    try:
        config, fonte = coletor.obter_config(url, prazo=prazo)
        if fonte != "TSE" and not _eleicoes_presidente(config):
            config, _ = coletor.obter_config(url, forcar=True, prazo=prazo)
    except Exception as erro:
        tipo, motivo = coletor.descrever_erro(erro)
        raise FeedFalhou("feed: configuração do TSE (ele-c.json)", tipo, f"{motivo} — {url}") from erro
    return config


def _eleicoes_presidente(config):
    achadas = {}
    for pleito, eleicao, cargo, _, _ in coletor.listar_alvos(config):
        if str(cargo["cd"]) == "1":
            achadas[str(eleicao["cd"])] = pleito["c"]
    return achadas


def eleicao_presidente(config):
    # (ciclo, código da eleição) — filtros TsePleito/TseTurno/TseEleicoes
    achadas = _eleicoes_presidente(config)
    if len(achadas) != 1:
        raise FeedFalhou(
            "feed: configuração do TSE (ele-c.json)", "tse_nao_encontrado",
            f"esperava 1 eleição de Presidente no pleito/turno/eleições configurados, achei "
            f"{', '.join(sorted(achadas)) or 'nenhuma'} — confira TsePleito/TseTurno/TseEleicoes")
    (eleicao, ciclo), = achadas.items()
    return ciclo, eleicao


def base_eleicao(ciclo, eleicao):
    return f"{coletor.TSE_BASE_URL}/{coletor.AMBIENTE}/{ciclo}/{eleicao}"


def url_dados(base, eleicao, uf, municipio=""):
    return f"{base}/dados/{uf}/{uf}{municipio}-c0001-e{eleicao.zfill(6)}-u.json"


def url_config_municipios(base, eleicao):
    return f"{base}/config/mun-e{eleicao.zfill(6)}-cm.json"


def nome_municipio(cdi, nome_tse):
    return NOMES_IBGE.get(cdi) or nome_tse.title()


# --- Ciclo rápido: agora.json, municipios-<uf>.json, historico.json -----------

class FeedRapido:
    # Estado em memória (Lambda quente): último valor bom por UF e por
    # município; na primeira execução vem dos arquivos já publicados.

    def __init__(self, destino, cliente, obter_config=config_via_coletor):
        self.destino = destino
        self.cliente = cliente
        self.obter_config = obter_config
        self.ultimo_uf = {}
        self.ultimo_municipio = {}
        self.semeado = False
        self.fotos_ok = set()
        self._pool = None

    def pool(self):
        # threads (e suas conexões com o TSE) sobrevivem entre ciclos
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=FEED_THREADS, thread_name_prefix="feed")
        return self._pool

    def _baixar_varios(self, urls, prazo):
        def um(url):
            try:
                return url, self.cliente.baixar(url, prazo).dados, None
            except Exception as erro:
                return url, None, erro
        return {url: (dados, erro) for url, dados, erro in self.pool().map(um, urls)}

    def _semear(self, numeros):
        # Lambda fria: último valor bom = arquivos publicados, se forem deste
        # turno/ambiente e dos mesmos candidatos (não herda dado de teste)
        self.semeado = True
        try:
            agora = self.destino.ler_json(FEED_PREFIXO + ARQ_AGORA)
            municipios = self.destino.ler_json(FEED_PREFIXO + ARQ_MUN_UF)
        except Exception as erro:
            logger.warning("Feed: não deu para ler os arquivos publicados (%s) — começando sem último valor bom",
                           coletor.descrever_erro(erro)[1])
            return
        if (agora and agora.get("turno") == int(coletor.TURNO) and agora.get("ambiente") == coletor.AMBIENTE
                and {c["numero"] for c in agora.get("candidatos", [])} == set(numeros)):
            self.ultimo_uf = {uf.lower(): t for uf, t in agora["presidente"]["uf"].items()}
            if municipios:
                self.ultimo_municipio = {m["tse"]: {k: v for k, v in m.items() if k not in ("ibge", "tse", "nome")}
                                         for m in municipios.get("municipios", [])}

    def _garantir_fotos(self, candidatos, base):
        for cand in candidatos:
            chave = f"{FEED_PREFIXO}fotos/{cand['sqcand']}.jpeg"
            if chave in self.fotos_ok:
                continue
            try:
                if not self.destino.existe(chave):
                    foto = baixar_bytes(f"{base}/fotos/br/{cand['sqcand']}.jpeg")
                    self.destino.gravar(chave, foto, "image/jpeg", CACHE_FOTO)
                    logger.info("Foto copiada: %s (%s)", cand["nome"], chave)
                self.fotos_ok.add(chave)
            except Exception as erro:
                # o feed segue; a próxima execução tenta de novo
                logger.warning("Feed: foto de %s não copiada: %s", cand["nome"], coletor.descrever_erro(erro)[1])

    def ciclo(self, prazo=None):
        inicio = time.monotonic()
        seq = int(time.time() * 1000)
        gerado = agora_brasilia().isoformat()
        self.cliente.zerar_contadores()

        ciclo_tse, eleicao = eleicao_presidente(self.obter_config(prazo))
        base = base_eleicao(ciclo_tse, eleicao)

        url_cm = url_config_municipios(base, eleicao)
        try:
            config_municipios = self.cliente.baixar(url_cm, prazo).dados
        except Exception as erro:
            config_municipios = self.cliente.ultimo_bom(url_cm)
            if config_municipios is None:
                raise FeedFalhou("feed: lista de municípios do TSE", *coletor.descrever_erro(erro)) from erro
        ufs = [abr["cd"] for abr in config_municipios["abr"]]
        municipios = [(mu["cd"], mu["cdi"], mu.get("nm", "")) for abr in config_municipios["abr"]
                      if abr["cd"] == UF for mu in abr["mu"]]

        url_br = url_dados(base, eleicao, "br")
        urls_uf = {uf: url_dados(base, eleicao, uf) for uf in ufs}
        urls_mun = {cd: url_dados(base, eleicao, UF, cd) for cd, _, _ in municipios}
        baixados = self._baixar_varios([url_br, *urls_uf.values(), *urls_mun.values()], prazo)

        dados_br, erro_br = baixados[url_br]
        if erro_br:
            # sem o Brasil não há candidatos nem total nacional: não grava
            raise FeedFalhou("feed: download do arquivo do TSE", *coletor.descrever_erro(erro_br),
                             alvo="Presidente (BR)") from erro_br

        candidatos = montar_candidatos(dados_br)
        numeros = [c["numero"] for c in candidatos]
        if not self.semeado:
            self._semear(numeros)
        falhas = []

        try:
            br = montar_totais(dados_br, numeros)
            por_uf = {}
            for uf, url in urls_uf.items():
                dados, erro = baixados[url]
                if dados is not None:
                    try:
                        self.ultimo_uf[uf] = montar_totais(dados, numeros)
                    except (KeyError, TypeError, ValueError) as erro_formato:
                        erro = erro_formato
                if erro is not None:
                    falhas.append((f"Presidente ({uf.upper()})", erro))
                    if uf not in self.ultimo_uf:
                        raise FeedFalhou("feed: download do arquivo do TSE", *coletor.descrever_erro(erro),
                                         alvo=f"Presidente ({uf.upper()}) — sem valor anterior") from erro
                por_uf[uf.upper()] = self.ultimo_uf[uf]

            lista_municipios = []
            for cd, cdi, nome_tse in sorted(municipios, key=lambda m: m[1]):
                dados, erro = baixados[urls_mun[cd]]
                if dados is not None:
                    try:
                        self.ultimo_municipio[cd] = montar_municipio(dados, numeros)
                    except (KeyError, TypeError, ValueError) as erro_formato:
                        erro = erro_formato
                if erro is not None:
                    falhas.append((f"Presidente ({UF.upper()} {cd})", erro))
                valores = self.ultimo_municipio.get(cd) or municipio_zerado(numeros)
                lista_municipios.append({"ibge": cdi, "tse": cd, "nome": nome_municipio(cdi, nome_tse), **valores})
        except (KeyError, TypeError, ValueError) as erro:
            raise FeedFalhou("feed: leitura do arquivo do TSE", *coletor.descrever_erro(erro)) from erro

        agora = {
            "versao": 1,
            "seq": seq,
            "gerado": gerado,
            "fonte_atualizado": iso_tse(dados_br["dt"], dados_br["ht"]),
            "turno": int(coletor.TURNO),
            "ambiente": coletor.AMBIENTE,
            "recarregar": FEED_RECARREGAR,
            "aviso": FEED_AVISO,
            "candidatos": candidatos,
            "presidente": {"br": br, "uf": por_uf},
        }
        municipios_uf = {"versao": 1, "seq": seq, "gerado": gerado, "uf": UF.upper(), "municipios": lista_municipios}

        try:
            historico_anterior = self.destino.ler_json(FEED_PREFIXO + ARQ_HISTORICO)
        except Exception as erro:
            # sem o anterior, gravar agora apagaria o histórico
            raise FeedFalhou("feed: leitura do historico.json", *coletor.descrever_erro(erro)) from erro
        historico = atualizar_historico(historico_anterior, seq, gerado, br, por_uf[UF.upper()])

        try:
            contrato.validar_ciclo(agora, municipios_uf, historico, len(municipios))
        except contrato.ContratoInvalido as erro:
            raise FeedFalhou("feed: validação do contrato", "tse_conteudo_invalido", str(erro)) from erro

        self._garantir_fotos(candidatos, base)

        # agora.json por último: seq novo nele => os outros dois já existem
        for nome, doc, cache in ((ARQ_MUN_UF, municipios_uf, CACHE_MUNICIPIOS),
                                 (ARQ_HISTORICO, historico, CACHE_RAPIDO),
                                 (ARQ_AGORA, agora, CACHE_RAPIDO)):
            try:
                self.destino.gravar(FEED_PREFIXO + nome, json_bytes(doc), CONTENT_TYPE, cache)
            except (ClientError, BotoCoreError, OSError) as erro:
                raise FeedFalhou("feed: gravação no S3", *coletor.descrever_erro(erro), alvo=nome) from erro

        return {
            "seq": seq,
            "pst_br": round(fracao(br), 6),
            "eleicao": eleicao,
            "falhas": [rotulo for rotulo, _ in falhas],
            "motivos": sorted({coletor.descrever_erro(erro)[1] for _, erro in falhas}),
            "requisicoes": self.cliente.requisicoes,
            "nao_modificados": self.cliente.nao_modificados,
            "duracao_ms": int((time.monotonic() - inicio) * 1000),
        }

    def executar_ciclo(self, prazo=None):
        # um ciclo com log no padrão do coletor; nunca levanta exceção
        try:
            resumo = self.ciclo(prazo)
        except FeedFalhou as erro:
            registrar_falha(erro.etapa, erro.tipo, erro.motivo, erro.alvo)
            return {"status": "erro", "motivo": str(erro)}
        except Exception as erro:
            registrar_falha("feed: erro inesperado", *coletor.descrever_erro(erro))
            logger.exception("Feed: erro inesperado")
            return {"status": "erro", "motivo": str(erro)}

        if resumo["falhas"]:
            logger.warning("Feed: %d arquivo(s) do TSE falharam neste ciclo — usando o último valor bom: %s",
                           len(resumo["falhas"]), ", ".join(resumo["falhas"][:10]),
                           extra={"feed_falhas": resumo["falhas"][:50], "feed_motivos": resumo["motivos"]})
        logger.info("Feed gravado | seq %d | pst BR %.4f | %d falha(s) | %d req (%d sem mudança) | %d ms",
                    resumo["seq"], resumo["pst_br"], len(resumo["falhas"]), resumo["requisicoes"],
                    resumo["nao_modificados"], resumo["duracao_ms"],
                    extra={"feed_status": "gravado", "feed_seq": resumo["seq"], "feed_pst_br": resumo["pst_br"],
                           "feed_falhas_total": len(resumo["falhas"]), "feed_requisicoes": resumo["requisicoes"],
                           "feed_nao_modificados": resumo["nao_modificados"],
                           "feed_duracao_ms": resumo["duracao_ms"]})
        return {"status": "gravado", **resumo}


# --- Ciclo lento: municipios-br.json -----------------------------------------

class FeedMunicipios:
    # Varre os municípios do Brasil. Os arquivos {uf}-e{ELEICAO}-ab.json (27
    # requisições) trazem, por município, seções totalizadas e horário: só
    # baixa o arquivo do município quando isso mudou. O último valor bom de
    # cada município (com ETag para GET condicional) fica no S3, em
    # _controle/feed-municipios-<eleicao>.json; falha nunca zera.

    def __init__(self, destino, cliente, obter_config=config_via_coletor):
        self.destino = destino
        self.cliente = cliente
        self.obter_config = obter_config
        self._pool = None

    def pool(self):
        if self._pool is None:
            self._pool = ThreadPoolExecutor(max_workers=FEED_MUN_THREADS, thread_name_prefix="feed-mun")
        return self._pool

    def executar(self, prazo=None):
        inicio = time.monotonic()
        seq = int(time.time() * 1000)
        gerado = agora_brasilia().isoformat()
        self.cliente.zerar_contadores()

        ciclo_tse, eleicao = eleicao_presidente(self.obter_config(prazo))
        base = base_eleicao(ciclo_tse, eleicao)
        e6 = eleicao.zfill(6)

        try:
            numeros = [c["numero"] for c in montar_candidatos(self.cliente.baixar(url_dados(base, eleicao, "br"), prazo).dados)]
            config_municipios = self.cliente.baixar(url_config_municipios(base, eleicao), prazo).dados
        except Exception as erro:
            raise FeedFalhou("feed: candidatos/municípios do TSE", *coletor.descrever_erro(erro)) from erro
        municipios = [(abr["cd"], mu["cd"], mu["cdi"]) for abr in config_municipios["abr"]
                      if abr["cd"] != "zz" for mu in abr["mu"]]

        chave_estado = f"_controle/feed-municipios-{eleicao}.json"
        try:
            estado = self.destino.ler_json(chave_estado) or {}
        except Exception as erro:
            # sem o estado, os municípios que falharem iriam a zero
            raise FeedFalhou("feed: leitura do estado dos municípios", *coletor.descrever_erro(erro)) from erro
        if estado.get("numeros") != sorted(numeros):
            estado = {}
        valores = estado.get("municipios", {})

        # 1. abrangências: o que mudou desde a última varredura
        def abrangencia(uf):
            try:
                return uf, self.cliente.baixar(f"{base}/dados/{uf}/{uf}-e{e6}-ab.json", prazo).dados
            except Exception as erro:
                logger.warning("Feed municípios: %s-e%s-ab.json falhou (%s) — baixando os não finalizados da UF",
                               uf, e6, coletor.descrever_erro(erro)[1])
                return uf, None
        marcadores = {}
        for _, dados in self.pool().map(abrangencia, sorted({uf for uf, _, _ in municipios})):
            for item in (dados or {}).get("abr", []):
                if item.get("tpabr") == "mun":
                    marcadores[item["cdabr"]] = (f'{item.get("dt")} {item.get("ht")}', inteiro(item["s"].get("st")))

        pendentes = []
        for uf, cd, _ in municipios:
            atual, marcador = valores.get(cd), marcadores.get(cd)
            if atual is None or (marcador and marcador[0] != atual.get("m")) or (not marcador and not atual.get("f")):
                pendentes.append((uf, cd))
        # com o teto de requisições/s pode não caber tudo: primeiro quem nunca
        # foi baixado (está zerado no arquivo), depois o que mudou
        pendentes.sort(key=lambda pendente: pendente[1] in valores)

        # 2. municípios que mudaram (GET condicional com o ETag guardado)
        def municipio(pendente):
            uf, cd = pendente
            atual = valores.get(cd) or {}
            try:
                return cd, self.cliente.baixar(url_dados(base, eleicao, uf, cd), prazo, guardar=False,
                                               etag=atual.get("etag"), modificado_em=atual.get("lm")), None
            except Exception as erro:
                return cd, None, erro

        falhas = []
        adiados = 0
        for cd, resposta, erro in self.pool().map(municipio, pendentes):
            marcador = marcadores.get(cd)
            if isinstance(erro, coletor.PrazoEsgotado):
                adiados += 1  # não coube nesta execução: fica pendente para a próxima
                continue
            if erro is not None:
                falhas.append((cd, erro))
                continue
            atual = valores.get(cd)
            if resposta.modificado:
                try:
                    lido = montar_municipio(resposta.dados, numeros)
                except (KeyError, TypeError, ValueError) as erro_formato:
                    falhas.append((cd, erro_formato))
                    continue
                atual = {"s": lido["secoes"], "t": lido["totalizadas"], "e": lido["eleitorado"], "v": lido["votos"],
                         "etag": resposta.etag, "lm": resposta.modificado_em}
            # arquivo do município pode vir mais atrasado que o -ab.json:
            # só marca como visto quando já reflete o que o -ab anunciou
            atual["m"] = marcador[0] if marcador and atual["t"] >= marcador[1] else None
            atual["f"] = atual["s"] > 0 and atual["t"] >= atual["s"]
            valores[cd] = atual

        # 3. arquivo colunar, ordenado pelo código IBGE
        linhas = sorted(municipios, key=lambda m: m[2])
        zerado = {"s": 0, "t": 0, "e": 0, "v": dict.fromkeys(numeros, 0)}
        doc = {"versao": 1, "seq": seq, "gerado": gerado, "ibge": [], "secoes": [], "totalizadas": [],
               "eleitorado": [], "votos": {n: [] for n in numeros}}
        for _, cd, cdi in linhas:
            v = valores.get(cd) or zerado
            doc["ibge"].append(cdi)
            doc["secoes"].append(v["s"])
            doc["totalizadas"].append(v["t"])
            doc["eleitorado"].append(v["e"])
            for n in numeros:
                doc["votos"][n].append(v["v"].get(n, 0))

        try:
            contrato.validar_municipios_br(doc, set(numeros), len(municipios))
        except contrato.ContratoInvalido as erro:
            raise FeedFalhou("feed: validação do contrato", "tse_conteudo_invalido", str(erro)) from erro
        try:
            self.destino.gravar(FEED_PREFIXO + ARQ_MUN_BR, json_bytes(doc), CONTENT_TYPE, CACHE_MUNICIPIOS)
            self.destino.gravar(chave_estado, json_bytes({"numeros": sorted(numeros), "municipios": valores}),
                                CONTENT_TYPE, "no-store")
        except (ClientError, BotoCoreError, OSError) as erro:
            raise FeedFalhou("feed: gravação no S3", *coletor.descrever_erro(erro), alvo=ARQ_MUN_BR) from erro

        totalizadas, secoes = sum(doc["totalizadas"]), sum(doc["secoes"])
        return {
            "seq": seq,
            "eleicao": eleicao,
            "municipios": len(municipios),
            "pst": round(totalizadas / secoes, 6) if secoes else 0.0,
            "finalizados": sum(1 for v in valores.values() if v.get("f")),
            "baixados": len(pendentes) - len(falhas) - adiados,
            "adiados": adiados,
            "limitadas_429": self.cliente.limitadas,
            "falhas": [cd for cd, _ in falhas],
            "motivos": sorted({coletor.descrever_erro(erro)[1] for _, erro in falhas}),
            "requisicoes": self.cliente.requisicoes,
            "nao_modificados": self.cliente.nao_modificados,
            "duracao_ms": int((time.monotonic() - inicio) * 1000),
            # pico de memória do processo (Linux: KB)
            "memoria_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024),
        }

    def executar_com_log(self, prazo=None):
        try:
            resumo = self.executar(prazo)
        except FeedFalhou as erro:
            registrar_falha(erro.etapa, erro.tipo, erro.motivo, erro.alvo)
            return {"status": "erro", "motivo": str(erro)}
        except Exception as erro:
            registrar_falha("feed: erro inesperado (municípios)", *coletor.descrever_erro(erro))
            logger.exception("Feed municípios: erro inesperado")
            return {"status": "erro", "motivo": str(erro)}

        if resumo["falhas"]:
            logger.warning("Feed municípios: %d município(s) falharam — mantido o último valor bom",
                           len(resumo["falhas"]),
                           extra={"feed_falhas": resumo["falhas"][:50], "feed_motivos": resumo["motivos"]})
        logger.info("Feed municípios gravado | seq %d | %d municípios | pst %.4f | %d baixados, %d adiados, "
                    "%d req (%d sem mudança, %d com 429) | %d ms | %d MB",
                    resumo["seq"], resumo["municipios"], resumo["pst"], resumo["baixados"], resumo["adiados"],
                    resumo["requisicoes"], resumo["nao_modificados"], resumo["limitadas_429"],
                    resumo["duracao_ms"], resumo["memoria_mb"],
                    extra={"feed_status": "municipios-gravado",
                           **{f"feed_{k}": v for k, v in resumo.items() if k not in ("falhas", "motivos")},
                           "feed_falhas_total": len(resumo["falhas"])})
        return {"status": "gravado", **resumo}


# --- Lambdas ------------------------------------------------------------------

_feed_rapido = None
_feed_municipios = None


def _prazo(context, padrao):
    restante = getattr(context, "get_remaining_time_in_millis", None)
    return time.monotonic() + (restante() / 1000 if restante else padrao) - FEED_MARGEM


def _com_lock(context, chave, ttl, rotulo, corpo):
    dono = getattr(context, "aws_request_id", None) or str(uuid.uuid4())
    try:
        conseguiu = coletor.adquirir_lock(dono, chave, ttl)
    except (ClientError, BotoCoreError) as erro:
        registrar_falha("feed: lock no S3", *coletor.descrever_erro(erro), alvo=rotulo)
        raise
    if not conseguiu:
        logger.warning("%s: execução anterior ainda rodando — execução ignorada.", rotulo,
                       extra={"feed_status": "skipped"})
        return {"status": "skipped"}
    try:
        return corpo()
    finally:
        coletor.liberar_lock(chave, ttl)


def handler_rapido(event, context):
    # Lambda coletor-tse-feed: ciclos a cada FEED_INTERVALO até FEED_JANELA
    global _feed_rapido
    if _feed_rapido is None:
        _feed_rapido = FeedRapido(DestinoS3(), ClienteTSE())
    prazo = _prazo(context, 60)

    def ciclos():
        inicio = time.monotonic()
        resultados = []
        while True:
            resultados.append(_feed_rapido.executar_ciclo(prazo)["status"])
            proximo = inicio + len(resultados) * FEED_INTERVALO
            if proximo - inicio > FEED_JANELA or proximo + FEED_INTERVALO / 3 > prazo:
                break
            time.sleep(max(0.0, proximo - time.monotonic()))
        return {"status": "ok", "ciclos": len(resultados), "gravados": resultados.count("gravado")}

    return _com_lock(context, FEED_LOCK_KEY, FEED_LOCK_TTL, "Feed", ciclos)


def handler_municipios(event, context):
    # Lambda coletor-tse-feed-municipios: uma varredura por execução
    global _feed_municipios
    if _feed_municipios is None:
        _feed_municipios = FeedMunicipios(DestinoS3(), ClienteTSE(por_segundo=FEED_MUN_REQ_POR_SEGUNDO))
    prazo = _prazo(context, 120)
    return _com_lock(context, FEED_MUN_LOCK_KEY, FEED_MUN_LOCK_TTL, "Feed municípios",
                     lambda: _feed_municipios.executar_com_log(prazo))
