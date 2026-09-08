"""
Setup para automaxia-utils - Pacote compartilhado entre projetos
"""
from setuptools import setup, find_packages
import os

# Ler README para descrição longa
with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

# Ler requirements
with open("requirements.txt", "r", encoding="utf-8") as fh:
    requirements = [line.strip() for line in fh if line.strip() and not line.startswith("#")]

setup(
    name="automaxia-utils",
    version="1.15.1",
    author="Automaxia",
    author_email="dev@automaxia.com",
    description="Utilitários compartilhados para rastreamento de tokens e integração com Admin Center",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/automaxia/automaxia-utils",
    packages=find_packages(exclude=["tests", "tests.*"]),
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Programming Language :: Python :: 3.13",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
    ],
    python_requires=">=3.8",
    install_requires=requirements,
    extras_require={
        "langchain": [
            "langchain>=0.1.0",
            "langchain-community>=0.0.13"
        ],
        "providers": [
            "anthropic>=0.25.0",
            "google-generativeai>=0.5.0"
        ],
        "database": [
            "psycopg2-binary>=2.9.0",
            "sqlalchemy>=2.0.0",
            "sshtunnel>=0.4.0"
        ],
        # `auth/middleware.py` (get_authenticated_user, RequirePermission) e o
        # unico modulo que importa FastAPI. Fica em EXTRA, nao em
        # install_requires, porque ha consumidor que usa so o
        # AdminCenterService/ConnectionResolver e nao roda FastAPI — obriga-lo
        # a instalar o framework inteiro seria peso morto.
        "fastapi": [
            "fastapi>=0.100.0",
            # `middleware.py` tenta `from jose import jwt` e cai para PyJWT —
            # suporte duplo deliberado, mas o CI nao tinha NENHUM dos dois e 3
            # testes de validacao local de token falhavam. Declaramos o jose,
            # que e o mesmo que o admincenter-api usa.
            "python-jose[cryptography]>=3.3.0"
        ],
        "all": [
            "langchain>=0.1.0",
            "langchain-community>=0.0.13",
            "anthropic>=0.25.0",
            "google-generativeai>=0.5.0",
            "psycopg2-binary>=2.9.0",
            "sqlalchemy>=2.0.0",
            "sshtunnel>=0.4.0",
            # Sem isto o CI quebrava na COLETA de test_auth_middleware.py e
            # test_rbac_helpers.py com "No module named 'fastapi'" — o
            # workflow instala `.[all]` e o extra nao trazia o framework.
            "fastapi>=0.100.0",
            "python-jose[cryptography]>=3.3.0"
        ],
        "dev": [
            "pytest>=7.0.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "flake8>=6.0.0",
            "mypy>=1.0.0",
            "twine>=4.0.0"
        ]
    },
    include_package_data=True,
    zip_safe=False,
)