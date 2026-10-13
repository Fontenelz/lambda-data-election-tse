import gzip
import http.server
import json
import shutil
import tempfile
import threading
import time
import unittest
from functools import partial
from pathlib import Path
from unittest import mock

from carregar import RAIZ, carregar_feed, client_error

# Ciclos do feed contra um "TSE" local: servidor HTTP servindo as fixtures
# reais do 1º turno (tests/fixtures/tse, mesmo caminho do TSE). Cada teste
# trabalha numa cópia, para poder alterar/remover arquivos.

FIXTURES = RAIZ / "tests/fixtures/tse"
ELE = "oficial/ele2026/6257"


class TSE(http.server.SimpleHTTPRequestHandler):
    falhas = {}  # caminho -> status HTTP forçado
    gzip = False

    def log_message(self, *args):
        pass

    def do_GET(self):
        status = self.falhas.get(self.path.lstrip("/"))
        if status:
            self.send_error(status)
            return
        if self.gzip and "gzip" in self.headers.get("Accept-Encoding", ""):
            caminho = Path(self.translate_path(self.path))
            corpo = gzip.compress(caminho.read_bytes())
            self.send_response(200)
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)
            return
        super().do_GET()


class ServidorTSE:
    def __init__(self):
        self.pasta = Path(tempfile.mkdtemp())
        shutil.copytree(FIXTURES, self.pasta, dirs_exist_ok=True)
        self.handler = type("TSETeste", (TSE,), {"falhas": {}})
        self.servidor = http.server.ThreadingHTTPServer(("127.0.0.1", 0),
                                                        partial(self.handler, directory=str(self.pasta)))
        threading.Thread(target=self.servidor.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.servidor.server_port}"

    def arquivo(self, caminho):
        return self.pasta / caminho

    def editar(self, caminho, funcao):
        alvo = self.arquivo(caminho)
        dados = json.loads(alvo.read_text("utf-8"))
        funcao(dados)
        alvo.write_text(json.dumps(dados), "utf-8")
        # Last-Modified tem resolução de segundo: garante "mudou" para o 304
        futuro = time.time() + 5
        import os
        os.utime(alvo, (futuro, futuro))

    def fechar(self):
        self.servidor.shutdown()
        self.servidor.server_close()
        shutil.rmtree(self.pasta, ignore_errors=True)


AMBIENTE = {"BUCKET_NAME": "bucket-teste", "TSE_AMBIENTE": "oficial", "TSE_TURNO": "1", "TSE_PLEITO": "3220",
            "TSE_ELEICOES": "6257", "TSE_UF": "ma", "FEED_PREFIXO": "aovivo/teste/",
            "FEED_BASE_URL": "https://cdn.teste", "FEED_CORES": '{"13": "#C0392B", "22": "#2543F4"}',
            "FEED_HTTP_TIMEOUT": "2", "FEED_HTTP_TENTATIVAS": "2"}


class Base(unittest.TestCase):
    def setUp(self):
        self.tse = ServidorTSE()
        self.addCleanup(self.tse.fechar)
        self.feed = carregar_feed({**AMBIENTE, "TSE_BASE_URL": self.tse.url})
        self.coletor = self.feed.coletor
        self.saida = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.saida, True)
        self.destino = self.feed.DestinoPasta(self.saida)
        self.gravados = []
        gravar = self.destino.gravar
        self.destino.gravar = lambda chave, *a, **k: (self.gravados.append(chave), gravar(chave, *a, **k))[1]
        mock.patch.object(self.feed, "baixar_bytes", return_value=b"jpeg").start()
        self.addCleanup(mock.patch.stopall)

    def config(self, prazo=None):
        return json.loads(self.tse.arquivo("oficial/comum/config/ele-c.json").read_text("utf-8"))

    def ler(self, nome):
        return json.loads((self.saida / "aovivo/teste" / nome).read_text("utf-8"))


class Leitura(unittest.TestCase):
    feed = carregar_feed({"BUCKET_NAME": "bucket-teste", "FEED_CORES": '{"13": "#C0392B"}'})
    br = json.loads((FIXTURES / ELE / "dados/br/br-c0001-e006257-u.json").read_text("utf-8"))

    def test_totais_do_brasil_batem_com_o_tse(self):
        numeros = [c["numero"] for c in self.feed.montar_candidatos(self.br)]
        totais = self.feed.montar_totais(self.br, numeros)
        self.assertEqual(totais["secoes"], 499248)
        self.assertEqual(totais["eleitorado"], 158745502)
        self.assertEqual(totais["validos"], 119300788)
        self.assertEqual(totais["nulos"], 3674249)  # tvn: inclui nulos técnicos
        self.assertEqual(totais["votos"]["13"], 53879538)
        self.assertEqual(totais["situacao"], "encerrada")
        self.assertEqual(len(totais["votos"]), 12)

    def test_e_s_no_primeiro_turno_nao_e_eleito(self):
        # Lula e Flávio têm e="s" com st="2º turno"
        numeros = [c["numero"] for c in self.feed.montar_candidatos(self.br)]
        self.assertIsNone(self.feed.montar_totais(self.br, numeros)["eleito"])
        self.assertTrue(self.feed.foi_eleito({"e": "s", "st": "Eleito"}))
        self.assertTrue(self.feed.foi_eleito({"e": "s", "st": "Matematicamente eleito"}))
        self.assertFalse(self.feed.foi_eleito({"e": "n", "st": "Não eleito"}))

    def test_candidatos(self):
        candidatos = self.feed.montar_candidatos(self.br)
        self.assertEqual([c["numero"] for c in candidatos[:2]], ["22", "13"])  # ordem seq do TSE
        lula = candidatos[1]
        self.assertEqual(lula, {"numero": "13", "nome": "LULA", "partido": "PT", "vice": "GERALDO ALCKMIN",
                                "sqcand": "280002542548",
                                "foto": "https://d3nthtia8j7edn.cloudfront.net/aovivo/2026/t2/fotos/280002542548.jpeg",
                                "cor": "#C0392B"})
        self.assertIsNone(candidatos[0]["cor"])

    def test_situacao(self):
        self.assertEqual(self.feed.situacao(0, 0), "nao-iniciada")
        self.assertEqual(self.feed.situacao(10, 0), "nao-iniciada")
        self.assertEqual(self.feed.situacao(10, 5), "apurando")
        self.assertEqual(self.feed.situacao(10, 10), "encerrada")

    def test_iso_tse(self):
        self.assertEqual(self.feed.iso_tse("05/10/2026", "12:51:05"), "2026-10-05T12:51:05-03:00")

    def test_hora_fonte_antes_da_totalizacao(self):
        # 2º turno antes da eleição: TSE manda dt/ht vazios (visto em 13/10/2026, 6258)
        dados = {"dg": "13/10/2026", "hg": "10:56:48", "dt": "", "ht": ""}
        self.assertEqual(self.feed.hora_fonte(dados), "2026-10-13T10:56:48-03:00")
        dados.update(dt="25/10/2026", ht="18:42:01")
        self.assertEqual(self.feed.hora_fonte(dados), "2026-10-25T18:42:01-03:00")


class Historico(unittest.TestCase):
    feed = carregar_feed({"BUCKET_NAME": "bucket-teste", "FEED_HISTORICO_MAX": "5"})

    def totais(self, totalizadas, votos=None):
        return {"secoes": 100, "totalizadas": totalizadas, "votos": votos or {"13": totalizadas, "22": 1}}

    def test_ponto_novo_so_quando_totalizadas_muda(self):
        h = None
        for seq, (br, ma) in enumerate([(1, 1), (1, 1), (2, 1), (2, 1), (2, 3)], start=1):
            h = self.feed.atualizar_historico(h, seq, f"t{seq}", self.totais(br), self.totais(ma))
        self.assertEqual(h["seq"], 5)
        self.assertEqual([p["t"] for p in h["pontos"]], ["t1", "t3", "t5"])
        self.assertEqual(h["pontos"][-1]["ma"]["pst"], 0.03)

    def test_limite_mantem_o_primeiro(self):
        h = None
        for i in range(1, 10):
            h = self.feed.atualizar_historico(h, i, f"t{i}", self.totais(i), self.totais(i))
        self.assertEqual([p["t"] for p in h["pontos"]], ["t1", "t6", "t7", "t8", "t9"])

    def test_recomeca_com_outros_candidatos(self):
        h = self.feed.atualizar_historico(None, 1, "t1", self.totais(1, {"13": 1, "22": 1, "70": 1}), self.totais(1))
        h = self.feed.atualizar_historico(h, 2, "t2", self.totais(1), self.totais(1))
        self.assertEqual([p["t"] for p in h["pontos"]], ["t2"])


class CicloRapido(Base):
    def rapido(self):
        return self.feed.FeedRapido(self.destino, self.feed.ClienteTSE(), self.config)

    def test_grava_os_tres_arquivos_validos_na_ordem(self):
        resultado = self.rapido().executar_ciclo()
        self.assertEqual(resultado["status"], "gravado", resultado)
        self.assertEqual([g for g in self.gravados if g.endswith(".json")],
                         ["aovivo/teste/municipios-ma.json", "aovivo/teste/historico.json", "aovivo/teste/agora.json"])
        agora, mun, hist = self.ler("agora.json"), self.ler("municipios-ma.json"), self.ler("historico.json")
        self.assertEqual(agora["seq"], mun["seq"])
        self.assertEqual(agora["seq"], hist["seq"])
        self.assertEqual(sorted(agora["presidente"]["uf"]), sorted(
            ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE",
             "PI", "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO", "ZZ"]))
        self.assertEqual(agora["presidente"]["uf"]["ZZ"]["eleitorado"], 916534)
        self.assertEqual(len(mun["municipios"]), 217)
        sao_luis = next(m for m in mun["municipios"] if m["tse"] == "09210")
        self.assertEqual((sao_luis["ibge"], sao_luis["nome"], sao_luis["votos"]["13"]), ("2111300", "São Luís", 337598))
        self.assertEqual(agora["candidatos"][1]["foto"], "https://cdn.teste/aovivo/teste/fotos/280002542548.jpeg")
        self.assertIn("aovivo/teste/fotos/280002542548.jpeg", self.gravados)
        self.assertEqual(len(hist["pontos"]), 1)

    def test_segundo_ciclo_usa_get_condicional(self):
        feed = self.rapido()
        primeiro = feed.executar_ciclo()
        segundo = feed.executar_ciclo()
        self.assertEqual(segundo["status"], "gravado")
        self.assertGreater(segundo["seq"], primeiro["seq"])
        self.assertEqual(segundo["nao_modificados"], segundo["requisicoes"])  # tudo 304
        self.assertEqual(len(self.ler("historico.json")["pontos"]), 1)  # nada mudou: sem ponto novo

    def test_ponto_novo_no_historico_quando_ma_muda(self):
        feed = self.rapido()
        feed.executar_ciclo()
        self.tse.editar(f"{ELE}/dados/ma/ma-c0001-e006257-u.json", lambda d: d["s"].update(st="1"))
        self.assertEqual(feed.executar_ciclo()["status"], "gravado")
        pontos = self.ler("historico.json")["pontos"]
        self.assertEqual(len(pontos), 2)
        self.assertEqual(self.ler("agora.json")["presidente"]["uf"]["MA"]["situacao"], "apurando")

    def test_gzip(self):
        self.tse.handler.gzip = True
        self.assertEqual(self.rapido().executar_ciclo()["status"], "gravado")

    def test_brasil_falhando_nao_grava(self):
        feed = self.rapido()
        feed.executar_ciclo()
        antes = self.ler("agora.json")
        self.tse.handler.falhas[f"{ELE}/dados/br/br-c0001-e006257-u.json"] = 503
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(feed.executar_ciclo()["status"], "erro")
        self.assertEqual(self.ler("agora.json"), antes)
        self.assertIn("Presidente (BR)", logs.output[0])

    def test_uf_falhando_usa_ultimo_valor_bom(self):
        feed = self.rapido()
        feed.executar_ciclo()
        valor_bom = self.ler("agora.json")["presidente"]["uf"]["SP"]
        self.tse.handler.falhas[f"{ELE}/dados/sp/sp-c0001-e006257-u.json"] = 404
        with self.assertLogs(level="WARNING"):
            resultado = feed.executar_ciclo()
        self.assertEqual(resultado["status"], "gravado")
        self.assertEqual(resultado["falhas"], ["Presidente (SP)"])
        self.assertEqual(self.ler("agora.json")["presidente"]["uf"]["SP"], valor_bom)

    def test_uf_sem_valor_anterior_nao_grava(self):
        self.tse.handler.falhas[f"{ELE}/dados/sp/sp-c0001-e006257-u.json"] = 404
        with self.assertLogs(level="ERROR"):
            self.assertEqual(self.rapido().executar_ciclo()["status"], "erro")
        self.assertFalse((self.saida / "aovivo/teste/agora.json").exists())

    def test_municipio_nunca_baixado_entra_zerado(self):
        self.tse.handler.falhas[f"{ELE}/dados/ma/ma09210-c0001-e006257-u.json"] = 503
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.rapido().executar_ciclo()["status"], "gravado")
        sao_luis = next(m for m in self.ler("municipios-ma.json")["municipios"] if m["tse"] == "09210")
        self.assertEqual((sao_luis["totalizadas"], sao_luis["votos"]["13"]), (0, 0))

    def test_dado_invalido_do_tse_nao_grava(self):
        feed = self.rapido()
        feed.executar_ciclo()
        antes = self.ler("agora.json")
        self.tse.editar(f"{ELE}/dados/ma/ma-c0001-e006257-u.json", lambda d: d["s"].update(st="999999"))
        with self.assertLogs(level="ERROR") as logs:
            self.assertEqual(feed.executar_ciclo()["status"], "erro")
        self.assertIn("validação do contrato", logs.output[0])
        self.assertEqual(self.ler("agora.json"), antes)

    def test_lambda_fria_herda_ultimo_valor_publicado(self):
        self.rapido().executar_ciclo()
        valor_bom = self.ler("agora.json")["presidente"]["uf"]["SP"]
        self.tse.handler.falhas[f"{ELE}/dados/sp/sp-c0001-e006257-u.json"] = 503
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.rapido().executar_ciclo()["status"], "gravado")  # instância nova
        self.assertEqual(self.ler("agora.json")["presidente"]["uf"]["SP"], valor_bom)

    def test_eleicao_errada_vira_erro_explicado(self):
        with mock.patch.object(self.coletor, "ELEICOES", {"6258"}), self.assertLogs(level="ERROR") as logs:
            self.assertEqual(self.rapido().executar_ciclo()["status"], "erro")
        self.assertIn("TsePleito/TseTurno/TseEleicoes", logs.output[0])


class CicloMunicipios(Base):
    # cm só com o MA: a varredura do Brasil inteiro, em escala de teste
    def setUp(self):
        super().setUp()
        self.tse.editar(f"{ELE}/config/mun-e006257-cm.json",
                        lambda d: d.update(abr=[a for a in d["abr"] if a["cd"] in ("ma", "zz")]))
        self.municipios = self.feed.FeedMunicipios(self.destino, self.feed.ClienteTSE(), self.config)

    def test_primeira_varredura_e_seguintes(self):
        primeira = self.municipios.executar()
        self.assertEqual((primeira["municipios"], primeira["baixados"], primeira["falhas"]), (217, 217, []))
        doc = self.ler("municipios-br.json")
        self.assertEqual(len(doc["ibge"]), 217)
        self.assertEqual(doc["ibge"], sorted(doc["ibge"]))
        i = doc["ibge"].index("2111300")
        self.assertEqual((doc["secoes"][i], doc["votos"]["13"][i]), (2273, 337598))

        segunda = self.feed.FeedMunicipios(self.destino, self.feed.ClienteTSE(), self.config).executar()
        self.assertEqual(segunda["baixados"], 0)  # -ab.json sem mudança: nada a baixar

        # São Luís muda no -ab e no arquivo do município: só ele é baixado
        self.tse.editar(f"{ELE}/dados/ma/ma-e006257-ab.json", lambda d: next(
            m for m in d["abr"] if m.get("cdabr") == "09210").update(ht="23:59:59"))
        self.tse.editar(f"{ELE}/dados/ma/ma09210-c0001-e006257-u.json", lambda d: next(
            c for a in d["carg"][0]["agr"] for p in a["par"] for c in p["cand"] if c["n"] == "13").update(vap="1"))
        terceira = self.municipios.executar()
        self.assertEqual(terceira["baixados"], 1)
        self.assertEqual(self.ler("municipios-br.json")["votos"]["13"][i], 1)

    def test_falha_nunca_zera(self):
        self.municipios.executar()
        self.tse.editar(f"{ELE}/dados/ma/ma-e006257-ab.json", lambda d: next(
            m for m in d["abr"] if m.get("cdabr") == "09210").update(ht="23:59:59"))
        self.tse.handler.falhas[f"{ELE}/dados/ma/ma09210-c0001-e006257-u.json"] = 503
        resultado = self.municipios.executar()
        self.assertEqual(resultado["falhas"], ["09210"])
        doc = self.ler("municipios-br.json")
        self.assertEqual(doc["votos"]["13"][doc["ibge"].index("2111300")], 337598)

    def test_arquivo_do_municipio_atrasado_em_relacao_ao_ab_e_baixado_de_novo(self):
        self.municipios.executar()
        # -ab anuncia mais seções totalizadas do que o arquivo do município já tem
        self.tse.editar(f"{ELE}/dados/ma/ma-e006257-ab.json", lambda d: next(
            m for m in d["abr"] if m.get("cdabr") == "09210").update(ht="23:59:59", s={"st": "9999"}))
        self.assertEqual(self.municipios.executar()["baixados"], 1)
        self.assertEqual(self.municipios.executar()["baixados"], 1)  # continua pendente até alcançar

    def test_ab_falhando_baixa_os_nao_finalizados(self):
        self.municipios.executar()
        self.tse.handler.falhas[f"{ELE}/dados/ma/ma-e006257-ab.json"] = 503
        with self.assertLogs(level="WARNING"):
            resultado = self.municipios.executar()
        self.assertEqual(resultado["baixados"], 0)  # 1º turno: todos já 100% totalizados


class LimiteDeTaxa(Base):
    URL_BR = f"{ELE}/dados/br/br-c0001-e006257-u.json"

    def test_429_nao_e_repetido_e_pausa_as_proximas(self):
        self.tse.handler.falhas[self.URL_BR] = 429
        cliente = self.feed.ClienteTSE()
        with mock.patch.object(self.feed, "FEED_PAUSA_429", 1.0):
            with self.assertRaises(self.feed.urllib.error.HTTPError):
                cliente.baixar(f"{self.tse.url}/{self.URL_BR}")
            self.assertEqual((cliente.requisicoes, cliente.limitadas), (1, 1))
            del self.tse.handler.falhas[self.URL_BR]
            inicio = time.monotonic()
            cliente.baixar(f"{self.tse.url}/{self.URL_BR}")
        self.assertGreaterEqual(time.monotonic() - inicio, 0.9)  # esperou a pausa

    def test_respeita_requisicoes_por_segundo(self):
        cliente = self.feed.ClienteTSE(por_segundo=20)
        inicio = time.monotonic()
        for _ in range(6):
            cliente.baixar(f"{self.tse.url}/{self.URL_BR}", guardar=False)
        self.assertGreaterEqual(time.monotonic() - inicio, 0.25)  # 5 intervalos de 50ms

    def test_sem_vez_antes_do_prazo_vira_prazo_esgotado(self):
        cliente = self.feed.ClienteTSE(por_segundo=1)
        cliente.baixar(f"{self.tse.url}/{self.URL_BR}", guardar=False)
        with self.assertRaises(self.coletor.PrazoEsgotado):
            cliente.baixar(f"{self.tse.url}/{self.URL_BR}", prazo=time.monotonic() + 1.5, guardar=False)

    def test_varredura_adia_o_que_nao_cabe_e_termina_depois(self):
        self.tse.editar(f"{ELE}/config/mun-e006257-cm.json",
                        lambda d: d.update(abr=[a for a in d["abr"] if a["cd"] in ("ma", "zz")]))
        cliente = self.feed.ClienteTSE(por_segundo=100)
        municipios = self.feed.FeedMunicipios(self.destino, cliente, self.config)
        primeira = municipios.executar(prazo=time.monotonic() + 2.5)
        self.assertEqual(primeira["falhas"], [])
        self.assertGreater(primeira["adiados"], 0)
        self.assertEqual(primeira["baixados"] + primeira["adiados"], 217)
        segunda = municipios.executar(prazo=time.monotonic() + 30)
        self.assertEqual((segunda["baixados"], segunda["adiados"]), (primeira["adiados"], 0))
        doc = self.ler("municipios-br.json")
        self.assertTrue(all(t > 0 for t in doc["totalizadas"]))


class Handlers(Base):
    def setUp(self):
        super().setUp()
        self.s3 = mock.patch.object(self.coletor, "s3").start()
        self.contexto = mock.Mock(aws_request_id="req-1")
        self.contexto.get_remaining_time_in_millis.return_value = 60_000

    def test_varios_ciclos_por_execucao_e_libera_o_lock(self):
        rapido = self.feed.FeedRapido(self.destino, self.feed.ClienteTSE(), self.config)
        mock.patch.object(self.feed, "_feed_rapido", rapido).start()
        mock.patch.multiple(self.feed, FEED_INTERVALO=0.05, FEED_JANELA=0.12).start()
        resultado = self.feed.handler_rapido({}, self.contexto)
        self.assertEqual((resultado["ciclos"], resultado["gravados"]), (3, 3))
        self.assertEqual(self.s3.put_object.call_args.kwargs["Key"], "_controle/feed.lock")
        self.s3.delete_object.assert_called_once_with(Bucket="bucket-teste", Key="_controle/feed.lock")

    def test_lock_proprio_ocupado_pula(self):
        self.s3.put_object.side_effect = client_error("PreconditionFailed")
        self.s3.head_object.return_value = {"LastModified": self.coletor.datetime.now(self.coletor.timezone.utc),
                                            "ETag": "x"}
        with self.assertLogs(level="WARNING"):
            self.assertEqual(self.feed.handler_municipios({}, self.contexto)["status"], "skipped")
        self.assertEqual(self.s3.put_object.call_args.kwargs["Key"], "_controle/feed-municipios.lock")
        self.s3.delete_object.assert_not_called()

    def test_destino_s3_grava_com_cache_e_content_type_do_contrato(self):
        rapido = self.feed.FeedRapido(self.feed.DestinoS3(), self.feed.ClienteTSE(), self.config)
        self.s3.get_object.side_effect = client_error("NoSuchKey")
        self.s3.head_object.return_value = {}
        self.assertEqual(rapido.executar_ciclo()["status"], "gravado")
        puts = {c.kwargs["Key"]: c.kwargs for c in self.s3.put_object.call_args_list}
        self.assertEqual(list(puts), ["aovivo/teste/municipios-ma.json", "aovivo/teste/historico.json",
                                      "aovivo/teste/agora.json"])
        # contrato: agora/historico max-age=5; municípios max-age=10
        cache = {"aovivo/teste/agora.json": "public, max-age=5, s-maxage=5, stale-while-revalidate=30, stale-if-error=600",
                 "aovivo/teste/historico.json": "public, max-age=5, s-maxage=5, stale-while-revalidate=30, stale-if-error=600",
                 "aovivo/teste/municipios-ma.json":
                     "public, max-age=10, s-maxage=5, stale-while-revalidate=30, stale-if-error=600"}
        for chave, put in puts.items():
            self.assertEqual(put["ContentType"], "application/json; charset=utf-8")
            self.assertEqual(put["CacheControl"], cache[chave], chave)
            json.loads(put["Body"])

        self.tse.editar(f"{ELE}/config/mun-e006257-cm.json",
                        lambda d: d.update(abr=[a for a in d["abr"] if a["cd"] in ("ma", "zz")]))
        municipios = self.feed.FeedMunicipios(self.feed.DestinoS3(), self.feed.ClienteTSE(), self.config)
        self.s3.put_object.reset_mock()
        with self.assertLogs(level="INFO"):
            self.assertEqual(municipios.executar_com_log()["status"], "gravado")
        put = next(c.kwargs for c in self.s3.put_object.call_args_list
                   if c.kwargs["Key"] == "aovivo/teste/municipios-br.json")
        self.assertEqual(put["CacheControl"],
                         "public, max-age=10, s-maxage=5, stale-while-revalidate=30, stale-if-error=600")


if __name__ == "__main__":
    unittest.main()
