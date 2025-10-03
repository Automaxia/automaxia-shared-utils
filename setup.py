from setuptools import setup, find_packages

setup(
    name="automaxia-utils",
    version="1.0.0",
    author="Automaxia",
    description="Utilitários compartilhados para projetos Automaxia",
    packages=find_packages(),
    install_requires=[
        "requests>=2.31.0",
        "python-decouple>=3.8",
        "tiktoken>=0.5.1",
    ],
    extras_require={
        "langchain": ["langchain>=0.1.0", "langchain-community>=0.0.13"]
    },
    python_requires=">=3.13",
)