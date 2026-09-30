import io
import json
import unittest
import urllib.error
from datetime import datetime, timezone
from unittest import mock

from carregar import carregar

notificador = carregar("notificador", {
    "TELEGRAM_CHAT_ID": "-100",
    "TELEGRAM_TOKEN_PARAM": "/coletor-tse/telegram-token",
    "AWS_REGION": "sa-east-1",
})


def alarme(nome, estado, anterior="OK"):
    return json.dumps({
        "AlarmName": nome,
        "NewStateValue": estado,
        "OldStateValue": anterior,
        "StateChangeTime": "2026-10-04T22:10:00.000+0000",
        "Trigger": {"Period": 300, "EvaluationPeriods": 1},
    })


class Utilitarios(unittest.TestCase):
    def test_duracao(self):
        self.assertEqual(notificador.duracao(10), "1 min")
        self.assertEqual(notificador.duracao(600), "10 min")
        self.assertEqual(notificador.duracao(3900), "1h05")

    def test_hora_em_brasilia(self):
        self.assertEqual(notificador.hora(datetime(2026, 10, 4, 22, 10, tzinfo=timezone.utc)), "04/10 19:10")


class Resumir(unittest.TestCase):
    def test_conta_execucoes_erros_e_quebras(self):
        eventos = [
            {"coleta_status": "success"},
            {"coleta_status": "partial"},
            {"level": "ERROR", "erro_etapa": "download do cargo", "erro_cargo": "Senador (MA)",
             "erro_tipo": "tse_indisponivel", "erro_motivo": "TSE instável (HTTP 503)"},
            {"type": "platform.report", "record": {"status": "timeout"}},
            {"type": "platform.report", "record": {"status": "error", "errorType": "ColetaFalhou"}},
        ]
        execucoes, erros, tipos = notificador.resumir(eventos)
        self.assertEqual(execucoes, {"success": 1, "partial": 1, "quebrou": 1})
        self.assertEqual(tipos, {"tse_indisponivel": 1, "quebrou": 1})
        self.assertIn(("download do cargo", "Senador (MA)", "TSE instável (HTTP 503)"), erros)


class Formatar(unittest.TestCase):
    def test_mensagem_manual_e_escapada(self):
        self.assertEqual(notificador.formatar("<teste>"), "&lt;teste&gt;")

    def test_alarm_com_contexto(self):
        with mock.patch.object(notificador, "linhas_de_contexto", return_value=["• contexto"]) as contexto:
            texto = notificador.formatar(alarme("coletor-tse-erros", "ALARM"))
        self.assertIn("Coletor TSE falhou", texto)
        self.assertIn("• contexto", texto)
        self.assertEqual(contexto.call_args.args[1].total_seconds(), 300)

    def test_falha_no_contexto_nao_impede_mensagem(self):
        with mock.patch.object(notificador, "linhas_de_contexto", side_effect=RuntimeError("x")):
            texto = notificador.formatar(alarme("coletor-tse-dados-parados", "ALARM"))
        self.assertIn("Dados do site parados", texto)
        self.assertIn("detalhes indisponíveis", texto)

    def test_ok_mostra_duracao_do_problema(self):
        inicio = datetime(2026, 10, 4, 21, 40, tzinfo=timezone.utc)
        with mock.patch.object(notificador, "inicio_do_problema", return_value=inicio):
            texto = notificador.formatar(alarme("coletor-tse-erros", "OK", anterior="ALARM"))
        self.assertIn("Problema durou <b>30 min</b>", texto)


class AlarmesCloudFront(unittest.TestCase):
    ARN = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:coletor-tse-cloudfront-4xx"

    def mensagem(self, estado, anterior="OK"):
        dados = json.loads(alarme("coletor-tse-cloudfront-4xx", estado, anterior))
        dados["AlarmArn"] = self.ARN
        return json.dumps(dados)

    def test_alarm_traz_dica_sem_consultar_logs_do_coletor(self):
        with mock.patch.object(notificador, "linhas_de_contexto") as contexto:
            texto = notificador.formatar(self.mensagem("ALARM"))
        contexto.assert_not_called()
        self.assertIn("CloudFront recusando requisições", texto)
        self.assertIn("AllowedOrigins", texto)
        self.assertIn("Monitoramento do CloudFront", texto)
        self.assertIn("https://us-east-1.console.aws.amazon.com/cloudwatch/", texto)
        self.assertNotIn("Logs do coletor", texto)

    def test_ok_busca_historico_na_regiao_do_alarme(self):
        with mock.patch.object(notificador, "inicio_do_problema", return_value=None) as inicio:
            notificador.formatar(self.mensagem("OK", anterior="ALARM"))
        self.assertEqual(inicio.call_args.args[2], "us-east-1")

    def test_regiao_padrao_sem_arn(self):
        self.assertEqual(notificador.regiao_do_alarme({}), "sa-east-1")


class Handler(unittest.TestCase):
    def evento(self, mensagem):
        return {"Records": [{"Sns": {"Message": mensagem, "Subject": "teste"}}]}

    def test_ok_sem_alarm_antes_nao_envia(self):
        with mock.patch.object(notificador, "enviar") as enviar:
            notificador.lambda_handler(self.evento(alarme("coletor-tse-erros", "OK", "INSUFFICIENT_DATA")), None)
        enviar.assert_not_called()

    def test_alarm_e_recuperacao_enviam(self):
        with mock.patch.object(notificador, "enviar") as enviar, \
                mock.patch.object(notificador, "formatar", return_value="x"):
            notificador.lambda_handler(self.evento(alarme("coletor-tse-erros", "ALARM")), None)
            notificador.lambda_handler(self.evento(alarme("coletor-tse-erros", "OK", "ALARM")), None)
            notificador.lambda_handler(self.evento("teste manual"), None)
        self.assertEqual(enviar.call_count, 3)


class Enviar(unittest.TestCase):
    def setUp(self):
        self.chamar = mock.patch.object(notificador, "chamar_telegram").start()
        mock.patch.object(notificador.time, "sleep").start()
        self.addCleanup(mock.patch.stopall)

    def erro(self, codigo, corpo):
        return urllib.error.HTTPError("https://api.telegram.org", codigo, "erro", {},
                                      io.BytesIO(json.dumps(corpo).encode()))

    def test_corta_no_limite(self):
        notificador.enviar("x" * 5000)
        self.assertLessEqual(len(self.chamar.call_args.args[0]["text"]), notificador.LIMITE_TELEGRAM)

    def test_html_invalido_vira_texto_puro(self):
        self.chamar.side_effect = [self.erro(400, {"description": "can't parse entities"}), None]
        notificador.enviar("<b>oi &amp; tchau")
        corpo = self.chamar.call_args.args[0]
        self.assertEqual(corpo["text"], "oi & tchau")
        self.assertNotIn("parse_mode", corpo)

    def test_429_espera_e_tenta_de_novo(self):
        self.chamar.side_effect = [self.erro(429, {"parameters": {"retry_after": 3}}), None]
        notificador.enviar("oi")
        self.assertEqual(self.chamar.call_count, 2)
        notificador.time.sleep.assert_called_once_with(3)


if __name__ == "__main__":
    unittest.main()
