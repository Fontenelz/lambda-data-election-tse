import json
import re
import shutil
import subprocess
import unittest

from carregar import RAIZ

# Roda a CloudFront Function do template.yaml no node, com a lista de origens
# preenchida como o !Sub faria. O runtime cloudfront-js-2.0 é um subconjunto
# de JavaScript que o node executa igual.

PERMITIDAS = ["https://imirante.com", "https://m.imirante.com"]


def codigo_da_funcao():
    template = (RAIZ / "template.yaml").read_text()
    bloco = re.search(r"FunctionCode: !Sub\n\s+- \|\n(.*?)\n\s+- Lista:", template, re.S).group(1)
    return bloco.replace("${Lista}", "', '".join(PERMITIDAS))


@unittest.skipUnless(shutil.which("node"), "node não instalado")
class OriginGuard(unittest.TestCase):
    def executar(self, metodo="GET", **cabecalhos):
        evento = {"request": {"method": metodo, "uri": "/dados_eleicao.json",
                              "headers": {k.replace("_", "-"): {"value": v} for k, v in cabecalhos.items()}}}
        script = f"{codigo_da_funcao()}\nconsole.log(JSON.stringify(handler({json.dumps(evento)})));"
        saida = subprocess.run(["node", "-e", script], capture_output=True, text=True, check=True).stdout
        return json.loads(saida)

    def test_origem_permitida_segue_para_o_s3(self):
        self.assertEqual(self.executar(origin="https://imirante.com")["uri"], "/dados_eleicao.json")

    def test_referer_permitido(self):
        self.assertIn("uri", self.executar(referer="https://m.imirante.com/eleicoes/2026"))

    def test_bloqueia_sem_origem_ou_com_origem_estranha(self):
        self.assertEqual(self.executar()["statusCode"], 403)
        self.assertEqual(self.executar(origin="https://www.imirante.com")["statusCode"], 403)
        self.assertEqual(self.executar(origin="https://imirante.com.evil.com")["statusCode"], 403)

    def test_preflight_respondido_na_borda(self):
        resposta = self.executar("OPTIONS", origin="https://imirante.com",
                                 access_control_request_method="GET",
                                 access_control_request_headers="cache-control")
        self.assertEqual(resposta["statusCode"], 204)
        cabecalhos = {k: v["value"] for k, v in resposta["headers"].items()}
        self.assertEqual(cabecalhos["access-control-allow-origin"], "https://imirante.com")
        self.assertEqual(cabecalhos["access-control-allow-headers"], "cache-control")
        self.assertIn("GET", cabecalhos["access-control-allow-methods"])

    def test_preflight_de_origem_estranha_continua_bloqueado(self):
        self.assertEqual(self.executar("OPTIONS", origin="https://evil.com")["statusCode"], 403)


if __name__ == "__main__":
    unittest.main()
