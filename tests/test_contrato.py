import copy
import json
import unittest

from carregar import RAIZ, carregar_feed

feed = carregar_feed({"BUCKET_NAME": "bucket-teste"})
contrato = feed.contrato

CM = json.loads((RAIZ / "tests/fixtures/tse/oficial/ele2026/6257/config/mun-e006257-cm.json").read_text("utf-8"))
NUMEROS = {"13", "22"}
UFS = ["AC", "AL", "AM", "AP", "BA", "CE", "DF", "ES", "GO", "MA", "MG", "MS", "MT", "PA", "PB", "PE", "PI",
       "PR", "RJ", "RN", "RO", "RR", "RS", "SC", "SE", "SP", "TO", "ZZ"]


def totais(totalizadas=10, secoes=20):
    return {"secoes": secoes, "totalizadas": totalizadas, "eleitorado": 100, "comparecimento": 80, "abstencao": 20,
            "validos": 70, "brancos": 4, "nulos": 6, "votos": {"13": 40, "22": 30}, "situacao": "apurando",
            "eleito": None}


def agora_valido():
    return {
        "versao": 1, "seq": 1792958530123, "gerado": "2026-10-25T18:42:10-03:00",
        "fonte_atualizado": "2026-10-25T18:42:01-03:00", "turno": 2, "ambiente": "oficial",
        "recarregar": None, "aviso": None,
        "candidatos": [{"numero": n, "nome": "Nome", "partido": "SG", "vice": "Vice", "sqcand": "280001234567",
                        "foto": f"https://exemplo/{n}.jpeg", "cor": None} for n in ("13", "22")],
        "presidente": {"br": totais(), "uf": {uf: totais() for uf in UFS}},
    }


def municipios_br_valido(n=3):
    return {"versao": 1, "seq": 1, "gerado": "2026-10-25T18:42:10-03:00",
            "ibge": [f"210000{i}" for i in range(n)], "secoes": [2] * n, "totalizadas": [1] * n,
            "eleitorado": [10] * n, "votos": {"13": [1] * n, "22": [0] * n}}


class Agora(unittest.TestCase):
    def test_valido(self):
        contrato.validar_agora(agora_valido())

    def test_rejeita(self):
        casos = {
            "totalizadas > secoes": lambda d: d["presidente"]["br"].update(totalizadas=21),
            "UF faltando": lambda d: d["presidente"]["uf"].pop("ZZ"),
            "campo inventado": lambda d: d.update(extra=1),
            "número como string": lambda d: d["presidente"]["br"].update(secoes="20"),
            "percentual": lambda d: d["presidente"]["br"].update(validos=70.5),
            "candidato sem voto": lambda d: d["presidente"]["uf"]["MA"]["votos"].pop("22"),
            "voto de quem não é candidato": lambda d: d["presidente"]["br"]["votos"].update({"45": 1}),
            "situação desconhecida": lambda d: d["presidente"]["br"].update(situacao="parcial"),
            "eleito fora da lista": lambda d: d["presidente"]["br"].update(eleito="45"),
            "gerado sem fuso -03:00": lambda d: d.update(gerado="2026-10-25T21:42:10Z"),
            "foto http": lambda d: d["candidatos"][0].update(foto="http://x/1.jpeg"),
            "cor inválida": lambda d: d["candidatos"][0].update(cor="vermelho"),
            "turno 3": lambda d: d.update(turno=3),
        }
        for nome, estragar in casos.items():
            with self.subTest(nome):
                doc = agora_valido()
                estragar(doc)
                with self.assertRaises(contrato.ContratoInvalido):
                    contrato.validar_agora(doc)


class MunicipiosBR(unittest.TestCase):
    def test_valido_e_eleitorado_opcional(self):
        contrato.validar_municipios_br(municipios_br_valido(), NUMEROS, 3)
        doc = municipios_br_valido()
        del doc["eleitorado"]
        contrato.validar_municipios_br(doc, NUMEROS, 3)

    def test_rejeita(self):
        casos = {
            "coluna curta": lambda d: d["totalizadas"].pop(),
            "votos curtos": lambda d: d["votos"]["22"].pop(),
            "quantidade errada": lambda d: (d["ibge"].pop(), d["secoes"].pop(), d["totalizadas"].pop(),
                                            d["eleitorado"].pop(), d["votos"]["13"].pop(), d["votos"]["22"].pop()),
            "ibge com 6 dígitos": lambda d: d["ibge"].__setitem__(0, "210000"),
            "ibge repetido": lambda d: d["ibge"].__setitem__(1, d["ibge"][0]),
            "totalizadas > secoes": lambda d: d["totalizadas"].__setitem__(0, 3),
        }
        for nome, estragar in casos.items():
            with self.subTest(nome):
                doc = municipios_br_valido()
                estragar(doc)
                with self.assertRaises(contrato.ContratoInvalido):
                    contrato.validar_municipios_br(doc, NUMEROS, 3)


class Ciclo(unittest.TestCase):
    def test_seq_tem_que_ser_igual(self):
        agora = agora_valido()
        municipios = {"versao": 1, "seq": agora["seq"], "gerado": agora["gerado"], "uf": "MA", "municipios": [
            {"ibge": "2111300", "tse": "09210", "nome": "São Luís", **{k: v for k, v in totais().items()
                                                                     if k not in ("abstencao", "validos", "situacao", "eleito")}}]}
        historico = {"versao": 1, "seq": agora["seq"], "pontos": []}
        contrato.validar_ciclo(agora, municipios, historico, 1)
        historico = dict(historico, seq=agora["seq"] - 1)
        with self.assertRaises(contrato.ContratoInvalido):
            contrato.validar_ciclo(agora, municipios, historico, 1)

    def test_historico_limitado_a_600(self):
        ponto = {"t": "2026-10-25T18:42:10-03:00", "br": {"pst": 0.5, "votos": {"13": 1, "22": 1}},
                 "ma": {"pst": 0.5, "votos": {"13": 1, "22": 1}}}
        contrato.validar_historico({"versao": 1, "seq": 1, "pontos": [ponto] * 600}, NUMEROS)
        with self.assertRaises(contrato.ContratoInvalido):
            contrato.validar_historico({"versao": 1, "seq": 1, "pontos": [ponto] * 601}, NUMEROS)
        with self.assertRaises(contrato.ContratoInvalido):
            contrato.validar_historico({"versao": 1, "seq": 1, "pontos": [dict(ponto, br={"pst": 1.5, "votos": {}})]},
                                       NUMEROS)


class MapeamentoIBGE(unittest.TestCase):
    # falha se algum município do TSE não casar com a tabela versionada
    def test_todos_os_municipios_do_tse_tem_ibge_e_nome(self):
        tse = {mu["cdi"] for abr in CM["abr"] if abr["cd"] != "zz" for mu in abr["mu"]}
        self.assertEqual(len(tse), 5571)
        self.assertEqual(tse, set(feed.NOMES_IBGE), "tabela coletor/municipios_ibge.json desatualizada — "
                                                     "rode ferramentas/gerar_municipios.py")
        self.assertTrue(all(len(cdi) == 7 and cdi.isdigit() for cdi in tse))

    def test_maranhao(self):
        ma = next(abr for abr in CM["abr"] if abr["cd"] == "ma")["mu"]
        self.assertEqual(len(ma), 217)
        self.assertTrue(all(cdi.startswith("21") for cdi in (m["cdi"] for m in ma)))
        self.assertEqual(feed.NOMES_IBGE["2111300"], "São Luís")

    def test_esquemas_sao_json_schema_draft7_validos(self):
        for arquivo in (RAIZ / "coletor/esquemas").glob("*.schema.json"):
            with self.subTest(arquivo.name):
                esquema = json.loads(arquivo.read_text("utf-8"))
                self.assertEqual(esquema["$schema"], "http://json-schema.org/draft-07/schema#")
                contrato.fastjsonschema.compile(copy.deepcopy(esquema))


if __name__ == "__main__":
    unittest.main()
