import copy
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from carregar import RAIZ, carregar_arquivo, client_error

# Não regressão do espelho: com os parâmetros de produção de hoje, o
# coletor/index.py novo grava no S3 exatamente o mesmo que a cópia do código
# em produção (tests/legado/index_producao.py) — mesmas chaves, bytes,
# Content-Type e Cache-Control —, a partir do ele-c.json oficial real.

PRODUCAO = {
    "BUCKET_NAME": "raw-tse-data-2026",
    "TSE_BASE_URL": "https://resultados.tse.jus.br",
    "TSE_AMBIENTE": "oficial",
    "TSE_PLEITO": "3220",
    "TSE_TURNO": "1",
    "TSE_UF": "ma",
}

CONFIG_OFICIAL = json.loads((RAIZ / "tests/fixtures/tse/oficial/comum/config/ele-c.json").read_text("utf-8"))

legado = carregar_arquivo(RAIZ / "tests/legado/index_producao.py", "coletor_producao", PRODUCAO)
novo = carregar_arquivo(RAIZ / "coletor/index.py", "coletor_novo", PRODUCAO)


def tse_falso(hora):
    # conteúdo determinístico por URL (o espelho grava os bytes como vieram)
    def baixar(url, prazo=None):
        dados = {"url": url, "dg": "05/10/2026", "hg": hora}
        return dados, json.dumps(dados).encode()
    return baixar


def gravacoes(modulo, config, hora="12:51:47", indice_anterior=None, ambiente=None):
    s3 = mock.MagicMock()
    if indice_anterior is None:
        s3.get_object.side_effect = client_error("NoSuchKey")
    else:
        s3.get_object.return_value = {"Body": io.BytesIO(indice_anterior)}
    with mock.patch.object(modulo, "s3", s3), \
            mock.patch.object(modulo, "obter_config", return_value=(copy.deepcopy(config), "TSE")), \
            mock.patch.object(modulo, "baixar_json", side_effect=tse_falso(hora)), \
            mock.patch.multiple(modulo, **(ambiente or {"UF": modulo.UF})):
        resultado = modulo.coletar()
    puts = []
    for chamada in s3.put_object.call_args_list:
        kw = dict(chamada.kwargs)
        if kw["Key"] == modulo.BLOB_NAME:
            indice = json.loads(kw["Body"])
            indice["atualizado_em"] = "<agora>"
            kw["Body"] = indice
        puts.append(kw)
    resultado.pop("atualizado_em")
    return resultado, puts


class EspelhoIgualAoDeProducao(unittest.TestCase):
    def test_primeira_coleta(self):
        self.assertEqual(gravacoes(novo, CONFIG_OFICIAL), gravacoes(legado, CONFIG_OFICIAL))

    def test_coleta_seguinte_com_e_sem_mudanca(self):
        _, puts = gravacoes(legado, CONFIG_OFICIAL)
        indice = next(p for p in puts if p["Key"] == "dados_eleicao.json")["Body"]
        anterior = json.dumps(indice).encode()
        for hora in ("12:51:47", "13:00:00"):  # sem mudança / tudo mudou
            with self.subTest(hora=hora):
                self.assertEqual(gravacoes(novo, CONFIG_OFICIAL, hora, anterior),
                                 gravacoes(legado, CONFIG_OFICIAL, hora, anterior))

    def test_caminhos_espelhados_de_hoje(self):
        _, puts = gravacoes(novo, CONFIG_OFICIAL)
        self.assertEqual([p["Key"] for p in puts], [
            "oficial/ele2026/6257/dados/br/br-c0001-e006257-u.json",
            "oficial/ele2026/6259/dados/ma/ma-c0003-e006259-u.json",
            "oficial/ele2026/6259/dados/ma/ma-c0005-e006259-u.json",
            "oficial/ele2026/6259/dados/ma/ma-c0006-e006259-u.json",
            "oficial/ele2026/6259/dados/ma/ma-c0007-e006259-u.json",
            "dados_eleicao.json",
        ])
        self.assertTrue(all(p["CacheControl"] == "public, max-age=0, s-maxage=30, stale-while-revalidate=60"
                            for p in puts))

    def test_tse_eleicoes_explicito_igual_a_vazio(self):
        self.assertEqual(gravacoes(novo, CONFIG_OFICIAL, ambiente={"ELEICOES": {"6257", "6259"}}),
                         gravacoes(novo, CONFIG_OFICIAL))


class SegundoTurno(unittest.TestCase):
    # parâmetros do dia: TsePleito=3221 TseTurno=2 TseEleicoes=6258,6260
    AMBIENTE = {"PLEITOS": {"3221"}, "TURNO": "2", "ELEICOES": {"6258", "6260"}}

    def test_so_presidente_para_o_ma(self):
        # 6260 (governador) não tem MA; suplementares/consultas do 3221 ficam de fora
        _, puts = gravacoes(novo, CONFIG_OFICIAL, ambiente=self.AMBIENTE)
        self.assertEqual([p["Key"] for p in puts],
                         ["oficial/ele2026/6258/dados/br/br-c0001-e006258-u.json", "dados_eleicao.json"])

    def test_tse_eleicoes_barra_eleicao_fora_da_lista(self):
        config = copy.deepcopy(CONFIG_OFICIAL)
        pleito = next(p for p in config["pl"] if p["cd"] == "3221")
        # eleição hipotética do 2º turno com abrangência br que não está na lista
        pleito["e"].append({"cd": "9999", "t": "2", "nm": "Intrusa", "abr": [
            {"cd": "br", "cp": [{"cd": "1", "ds": "Presidente"}]}]})
        _, puts = gravacoes(novo, config, ambiente=self.AMBIENTE)
        self.assertNotIn("9999", json.dumps([p["Key"] for p in puts]))
        _, puts = gravacoes(novo, config, ambiente={**self.AMBIENTE, "ELEICOES": set()})
        self.assertIn("oficial/ele2026/9999/dados/br/br-c0001-e009999-u.json", [p["Key"] for p in puts])


if __name__ == "__main__":
    unittest.main()
