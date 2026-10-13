import io
import json
import time
import unittest
import urllib.error
from unittest import mock

from carregar import carregar, client_error

coletor = carregar("coletor", {
    "BUCKET_NAME": "bucket-teste",
    "TSE_BASE_URL": "https://tse.teste/simulado",
    "TSE_AMBIENTE": "simulado2026",
    "TSE_TURNO": "1",
    "TSE_UF": "ma",
    "TSE_PLEITO": "",  # vazio = todos; o filtro por pleito tem testes próprios
})

BASE = "https://tse.teste/simulado/simulado2026/ele2026"


def config_tse(turno="1", como_numero=False):
    def cd(valor):
        return int(valor) if como_numero else valor

    return {"pl": [{"cd": cd("1"), "c": "ele2026", "e": [
        {"cd": cd("21270"), "t": cd(turno), "nm": "Federal", "abr": [
            {"cd": "br", "cp": [{"cd": cd("1"), "ds": "Presidente"}]}]},
        {"cd": cd("21272"), "t": cd(turno), "nm": "Estadual", "abr": [
            {"cd": "br", "cp": [{"cd": cd("3"), "ds": "Governador"},
                                {"cd": cd("5"), "ds": "Senador"},
                                {"cd": cd("11"), "ds": "Prefeito"}]}]},
    ]}]}


URL_PRESIDENTE = f"{BASE}/21270/dados/br/br-c0001-e021270-u.json"
URL_GOVERNADOR = f"{BASE}/21272/dados/ma/ma-c0003-e021272-u.json"
URL_SENADOR = f"{BASE}/21272/dados/ma/ma-c0005-e021272-u.json"


def resposta_tse(hora):
    dados = {"dg": "01/10/2026", "hg": hora}
    return dados, json.dumps(dados).encode()


def http_error(codigo):
    return urllib.error.HTTPError("https://tse.teste", codigo, "erro", {}, io.BytesIO(b""))


class DescreverErro(unittest.TestCase):
    def test_classifica_causas(self):
        casos = [
            (http_error(404), "tse_nao_encontrado"),
            (http_error(403), "tse_nao_encontrado"),
            (http_error(429), "tse_indisponivel"),
            (http_error(503), "tse_indisponivel"),
            (TimeoutError(), "tse_indisponivel"),
            (coletor.PrazoEsgotado(), "tse_indisponivel"),
            (urllib.error.URLError("dns"), "tse_indisponivel"),
            (json.JSONDecodeError("x", "", 0), "tse_conteudo_invalido"),
            (KeyError("pl"), "tse_conteudo_invalido"),
            (client_error("AccessDenied"), "s3"),
            (RuntimeError("?"), "inesperado"),
        ]
        for erro, tipo in casos:
            with self.subTest(erro=erro):
                self.assertEqual(coletor.descrever_erro(erro)[0], tipo)

    def test_prazo_esgotado_tem_motivo_proprio(self):
        self.assertIn("acabou o tempo", coletor.descrever_erro(coletor.PrazoEsgotado())[1])


class ExtrairTimestamp(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(coletor.extrair_timestamp_geracao({"dg": "d", "hg": "h"}), "d h")
        self.assertEqual(coletor.extrair_timestamp_geracao({"dt": "d", "ht": "h"}), "d h")
        self.assertIsNone(coletor.extrair_timestamp_geracao({"dg": "d"}))
        self.assertIsNone(coletor.extrair_timestamp_geracao([]))


class ListarAlvos(unittest.TestCase):
    def cargos(self, config):
        return [(str(cargo["cd"]), uf) for _, _, cargo, uf, _ in coletor.listar_alvos(config)]

    def test_filtra_turno_e_cargos_cobertos(self):
        self.assertEqual(self.cargos(config_tse()), [("1", "br"), ("3", "ma"), ("5", "ma")])
        self.assertEqual(self.cargos(config_tse(turno="2")), [])

    def test_aceita_codigos_numericos(self):
        self.assertEqual(self.cargos(config_tse(como_numero=True)), [("1", "br"), ("3", "ma"), ("5", "ma")])

    def test_preparar_alvo_com_codigos_numericos(self):
        alvos = list(coletor.listar_alvos(config_tse(como_numero=True)))
        item, url, rotulo = coletor.preparar_alvo(*alvos[1])
        self.assertEqual(url, URL_GOVERNADOR)
        self.assertEqual(item["arquivo"], "simulado2026/ele2026/21272/dados/ma/ma-c0003-e021272-u.json")
        self.assertEqual((item["eleicao"], item["cargo"]), ("21272", "3"))
        self.assertEqual(rotulo, "Governador (MA)")


    def test_filtra_pleito(self):
        with mock.patch.object(coletor, "PLEITOS", {"3220"}):
            self.assertEqual(self.cargos(config_tse()), [])
        with mock.patch.object(coletor, "PLEITOS", {"1"}):
            self.assertEqual(self.cargos(config_tse()), [("1", "br"), ("3", "ma"), ("5", "ma")])

    def test_ignora_abrangencia_de_outra_uf(self):
        # 2º turno de 2026: governador só em AM/RN/DF/ES/AC — MA não pode gerar alvo
        config = {"pl": [{"cd": "3221", "c": "ele2026", "e": [
            {"cd": "6260", "t": "2", "abr": [{"cd": uf, "cp": [{"cd": "3", "ds": "Governador"}]}
                                             for uf in ("am", "rn", "df", "es", "ac")]},
            {"cd": "6258", "t": "2", "abr": [{"cd": "br", "cp": [{"cd": "1", "ds": "Presidente"}]}]},
        ]}]}
        with mock.patch.object(coletor, "TURNO", "2"):
            self.assertEqual(self.cargos(config), [("1", "br")])

    def test_ciclo_vem_do_pleito_de_cada_eleicao(self):
        # ele-c.json oficial lista pleitos de ele2024 antes dos de ele2026
        config = {"pl": [
            {"cd": "452", "c": "ele2024", "e": [{"cd": "619", "t": "1", "abr": [
                {"cd": "br", "cp": [{"cd": "11", "ds": "Prefeito"}]}]}]},
            {"cd": "3220", "c": "ele2026", "e": [{"cd": "6257", "t": "1", "abr": [
                {"cd": "br", "cp": [{"cd": "1", "ds": "Presidente"}]}]}]},
        ]}
        item, url, _ = coletor.preparar_alvo(*next(coletor.listar_alvos(config)))
        self.assertEqual(item["arquivo"], "simulado2026/ele2026/6257/dados/br/br-c0001-e006257-u.json")
        self.assertIn("/ele2026/6257/", url)


class BaixarJson(unittest.TestCase):
    def setUp(self):
        self.sleep = mock.patch.object(coletor.time, "sleep").start()
        self.urlopen = mock.patch.object(coletor.urllib.request, "urlopen").start()
        self.addCleanup(mock.patch.stopall)

    def responder(self, corpo):
        resposta = mock.MagicMock()
        resposta.__enter__.return_value.read.return_value = corpo
        return resposta

    def test_tenta_de_novo_em_5xx(self):
        self.urlopen.side_effect = [http_error(502), self.responder(b'{"ok": 1}')]
        self.assertEqual(coletor.baixar_json(URL_PRESIDENTE)[0], {"ok": 1})
        self.assertEqual(self.urlopen.call_count, 2)
        self.sleep.assert_called_once_with(1)

    def test_404_falha_na_hora(self):
        self.urlopen.side_effect = http_error(404)
        with self.assertRaises(urllib.error.HTTPError):
            coletor.baixar_json(URL_PRESIDENTE)
        self.assertEqual(self.urlopen.call_count, 1)

    def test_json_incompleto_esgota_tentativas(self):
        self.urlopen.return_value = self.responder(b'{"ok": ')
        with self.assertRaises(ValueError):
            coletor.baixar_json(URL_PRESIDENTE)
        self.assertEqual(self.urlopen.call_count, coletor.HTTP_TENTATIVAS)

    def test_sem_prazo_nem_tenta(self):
        with self.assertRaises(coletor.PrazoEsgotado):
            coletor.baixar_json(URL_PRESIDENTE, prazo=time.monotonic() + 1)
        self.urlopen.assert_not_called()

    def test_timeout_encolhe_para_caber_no_prazo(self):
        self.urlopen.return_value = self.responder(b"{}")
        coletor.baixar_json(URL_PRESIDENTE, prazo=time.monotonic() + 5)
        self.assertLessEqual(self.urlopen.call_args.kwargs["timeout"], 5)

    def test_sem_tempo_para_nova_tentativa_devolve_erro_real(self):
        self.urlopen.side_effect = http_error(503)
        with self.assertRaises(urllib.error.HTTPError):
            coletor.baixar_json(URL_PRESIDENTE, prazo=time.monotonic() + coletor.HTTP_TIMEOUT_MINIMO + 0.5)
        self.assertEqual(self.urlopen.call_count, 1)
        self.sleep.assert_not_called()


class Coletar(unittest.TestCase):
    def setUp(self):
        self.s3 = mock.patch.object(coletor, "s3").start()
        mock.patch.object(coletor, "obter_config", return_value=(config_tse(), "TSE")).start()
        self.baixar = mock.patch.object(coletor, "baixar_json").start()
        self.addCleanup(mock.patch.stopall)
        self.indice = None
        self.s3.get_object.side_effect = client_error("NoSuchKey")

    def tse(self, respostas):
        def baixar(url, prazo=None):
            resposta = respostas[url]
            if isinstance(resposta, Exception):
                raise resposta
            return resposta
        self.baixar.side_effect = baixar

    def gravados(self):
        return {chamada.kwargs["Key"]: chamada.kwargs["Body"] for chamada in self.s3.put_object.call_args_list}

    def indice_anterior(self, itens):
        corpo = io.BytesIO(json.dumps({"resultados": itens}).encode())
        self.s3.get_object.side_effect = None
        self.s3.get_object.return_value = {"Body": corpo}

    def primeira_coleta(self):
        self.tse({URL_PRESIDENTE: resposta_tse("10:00"), URL_GOVERNADOR: resposta_tse("10:00"),
                  URL_SENADOR: resposta_tse("10:00")})
        coletor.coletar()
        itens = json.loads(self.gravados()["dados_eleicao.json"])["resultados"]
        self.s3.put_object.reset_mock()
        return itens

    def test_primeira_execucao_grava_tudo_na_ordem(self):
        itens = self.primeira_coleta()
        self.assertEqual([i["nome_cargo"] for i in itens], ["Presidente", "Governador", "Senador"])

    def test_sem_mudanca_nao_regrava(self):
        self.indice_anterior(self.primeira_coleta())
        resultado = coletor.coletar()
        self.assertEqual(resultado["status"], "success")
        self.assertFalse(resultado["mudou"])
        self.s3.put_object.assert_not_called()

    def test_so_regrava_o_que_mudou(self):
        self.indice_anterior(self.primeira_coleta())
        self.tse({URL_PRESIDENTE: resposta_tse("10:00"), URL_GOVERNADOR: resposta_tse("10:05"),
                  URL_SENADOR: resposta_tse("10:00")})
        coletor.coletar()
        self.assertEqual(set(self.gravados()), {
            "simulado2026/ele2026/21272/dados/ma/ma-c0003-e021272-u.json", "dados_eleicao.json"})

    def test_falha_de_um_cargo_mantem_item_anterior(self):
        self.indice_anterior(self.primeira_coleta())
        self.tse({URL_PRESIDENTE: resposta_tse("10:05"), URL_GOVERNADOR: http_error(503),
                  URL_SENADOR: resposta_tse("10:00")})
        resultado = coletor.coletar()
        self.assertEqual(resultado["status"], "partial")
        self.assertEqual(resultado["cargos_com_erro"], ["Governador (MA)"])
        itens = json.loads(self.gravados()["dados_eleicao.json"])["resultados"]
        self.assertEqual([i["nome_cargo"] for i in itens], ["Presidente", "Governador", "Senador"])

    def test_todos_falhando_vira_erro(self):
        self.tse({URL_PRESIDENTE: coletor.PrazoEsgotado(), URL_GOVERNADOR: coletor.PrazoEsgotado(),
                  URL_SENADOR: http_error(404)})
        with self.assertRaises(coletor.ColetaFalhou) as ctx:
            coletor.coletar()
        self.assertEqual(ctx.exception.tipo, "tse_indisponivel")

    def test_repassa_prazo_para_os_downloads(self):
        self.primeira_coleta()
        self.baixar.reset_mock()
        coletor.coletar(prazo=123.0)
        self.assertEqual({chamada.args[1] for chamada in self.baixar.call_args_list}, {123.0})


class LambdaHandler(unittest.TestCase):
    def setUp(self):
        self.s3 = mock.patch.object(coletor, "s3").start()
        self.coletar = mock.patch.object(coletor, "coletar").start()
        self.addCleanup(mock.patch.stopall)
        self.contexto = mock.Mock(aws_request_id="req-1")
        self.contexto.get_remaining_time_in_millis.return_value = 120_000

    def test_prazo_deixa_margem_antes_do_timeout(self):
        self.coletar.return_value = {"status": "success", "falhas": 0, "cargos_com_erro": []}
        antes = time.monotonic()
        coletor.lambda_handler({}, self.contexto)
        prazo = self.coletar.call_args.args[0]
        self.assertAlmostEqual(prazo - antes, 120 - coletor.MARGEM_SEGUNDOS, delta=1)
        self.s3.delete_object.assert_called_once()

    def test_libera_lock_mesmo_com_falha(self):
        self.coletar.side_effect = coletor.ColetaFalhou("etapa", "inesperado", "motivo")
        with self.assertRaises(coletor.ColetaFalhou):
            coletor.lambda_handler({}, self.contexto)
        self.s3.delete_object.assert_called_once()

    def test_lock_ocupado_pula_execucao(self):
        self.s3.put_object.side_effect = client_error("PreconditionFailed")
        self.s3.head_object.return_value = {"LastModified": coletor.datetime.now(coletor.timezone.utc),
                                            "ETag": "x"}
        self.assertEqual(coletor.lambda_handler({}, self.contexto)["status"], "skipped")
        self.coletar.assert_not_called()
        self.s3.delete_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
