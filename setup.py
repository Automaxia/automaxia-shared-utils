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
    version="1.0.0",
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