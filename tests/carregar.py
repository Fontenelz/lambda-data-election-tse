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
    _garantir_boto3()
    nome = f"{pasta}_index"
    spec = importlib.util.spec_from_file_location(nome, RAIZ / pasta / "index.py")
    modulo = importlib.util.module_from_spec(spec)
    with mock.patch.dict(os.environ, ambiente), mock.patch("boto3.client", return_value=mock.MagicMock()):
        spec.loader.exec_module(modulo)
    sys.modules[nome] = modulo
    return modulo


def client_error(codigo, operacao="Operacao"):
    import botocore.exceptions
    return botocore.exceptions.ClientError({"Error": {"Code": codigo, "Message": codigo}}, operacao)
