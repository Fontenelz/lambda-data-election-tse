import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest import mock

# Carrega coletor/index.py e notificador/index.py (os dois se chamam "index")
# com o boto3 trocado por mocks. Se boto3/botocore não estiverem instalados,
# cria módulos mínimos só com o que o código usa — os testes rodam com o
# Python puro: python3 -m unittest discover tests

RAIZ = Path(__file__).resolve().parent.parent


class ClientError(Exception):
    def __init__(self, error_response, operation_name):
        super().__init__(f"{operation_name}: {error_response}")
        self.response = error_response
        self.operation_name = operation_name


class BotoCoreError(Exception):
    pass


def _garantir_boto3():
    try:
        import boto3  # noqa: F401
        import botocore.exceptions  # noqa: F401
    except ImportError:
        botocore = types.ModuleType("botocore")
        excecoes = types.ModuleType("botocore.exceptions")
        excecoes.ClientError = ClientError
        excecoes.BotoCoreError = BotoCoreError
        botocore.exceptions = excecoes
        boto3 = types.ModuleType("boto3")
        boto3.client = lambda *args, **kwargs: None
        sys.modules.update({"boto3": boto3, "botocore": botocore, "botocore.exceptions": excecoes})


def carregar(pasta, ambiente):
    return carregar_arquivo(RAIZ / pasta / "index.py", f"{pasta}_index", ambiente)


def carregar_arquivo(caminho, nome, ambiente):
    _garantir_boto3()
    spec = importlib.util.spec_from_file_location(nome, caminho)
    modulo = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, ambiente), mock.patch("boto3.client", return_value=mock.MagicMock()):
        spec.loader.exec_module(modulo)
    sys.modules[nome] = modulo
    return modulo


def carregar_feed(ambiente):
    # feed.py importa "index" e "contrato" pelo nome, como na Lambda (raiz do
    # zip = coletor/). Recarrega os três com o ambiente pedido.
    _garantir_boto3()
    pasta = str(RAIZ / "coletor")
    if pasta not in sys.path:
        sys.path.insert(0, pasta)
    for nome in ("feed", "index", "contrato"):
        sys.modules.pop(nome, None)
    with mock.patch.dict(os.environ, ambiente), mock.patch("boto3.client", return_value=mock.MagicMock()):
        return importlib.import_module("feed")


def client_error(codigo, operacao="Operacao"):
    import botocore.exceptions
    return botocore.exceptions.ClientError({"Error": {"Code": codigo, "Message": codigo}}, operacao)
