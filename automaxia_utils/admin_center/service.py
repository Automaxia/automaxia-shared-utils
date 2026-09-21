"""
Admin Center Service - Versão Genérica Otimizada
Serviço universal para integração com Admin Center API
Compatível com múltiplos projetos - OTIMIZADO PARA PERFORMANCE ASSÍNCRONA
"""

import os
import json
import time
import logging
import requests
import uuid
from typing import Dict, Optional, Any, List
from datetime import datetime
from threading import Thread, Lock, local
from queue import Queue, Empty
from contextlib import contextmanager
from dataclasses import dataclass
from decouple import config
from uuid import UUID

@dataclass
class AdminCenterConfig:
    """Configuração genérica do Admin Center - OTIMIZADA PARA PERFORMANCE"""
    # Configurações obrigatórias
    api_url: str = ""
    api_url_local: str = ""
    api_key: str = ""
    product_id: str = ""
    environment_id: str = ""
    organization_id: str = ""
    
    # Configurações de ambiente
    environment_id_dev: str = ""
    environment_name: str = ""
    
    # Configurações opcionais - OTIMIZADAS PARA ALTA PERFORMANCE
    enabled: bool = True
    batch_mode: bool = True          # SEMPRE assíncrono para não impactar aplicação
    batch_size: int = 50             # Batch maior para eficiência
    batch_interval: int = 2          # Intervalo menor (2s) para responsividade
    timeout: int = 10                # Timeout menor para não travar
    max_retries: int = 2             # Menos retries para ser mais rápido
    queue_max_size: int = 1000       # Fila grande para alta carga

    # Identidade do produto consumidor. Preferido sobre `product_id`: com o
    # slug + a api-key da' para descobrir product_id/environment_id no proprio
    # AdminCenter (ver `_discover_environment_id_via_api`).
    product_slug: str = ""

    # Nivel MINIMO que vai para a tabela de logs do AdminCenter.
    #
    # `application_logs` vira rastro de execucao: a esmagadora maioria das
    # linhas e' INFO de processo ("Requisicao recebida", "Query executada",
    # "[CACHE HIT]") — que ja' existe no stdout do pod. No banco so' interessa
    # o que alguem vai investigar depois. WARNING+ derruba a maior parte do
    # volume e preserva o que importa.
    #
    # Nao mexe no logging local: o produto continua imprimindo INFO no stdout.
    # Para depurar um produto especifico em janela curta, baixe para INFO via
    # `ADMIN_CENTER_LOG_MIN_LEVEL` (variavel do produto no AdminCenter) e volte
    # depois — a mudanca vale no proximo start.
    log_min_level: str = "WARNING"

    @classmethod
    def from_env(cls, prefix: str = "ADMIN_CENTER"):
        """Cria configuração a partir de variáveis de ambiente"""
            
        environment = os.getenv("ENVIRONMENT", "production").lower()
        
        # URL principal (Prod)
        api_url = os.getenv(f"{prefix}_URL", "")
        
        # API key principal (Prod)
        api_key = os.getenv(f"{prefix}_API_KEY", "")

        # Suporte a URL/API key de desenvolvimento
        if environment == 'development':
            # URL (Prioridade para DEV_URL, depois LOCAL)
            dev_url = os.getenv(f"{prefix}_DEV_URL") or os.getenv(f"{prefix}_URL_LOCAL")
            if dev_url:
                api_url = dev_url

            # API key de teste tem prioridade; aceita sufixo minusculo ou
            # maiusculo (_test / _TEST). Se ausente, mantem a padrao.
            api_key = (
                os.getenv(f"{prefix}_API_KEY_test")
                or os.getenv(f"{prefix}_API_KEY_TEST")
                or api_key
            )

        return cls(
            api_url=api_url,
            api_key=api_key,
            product_id=os.getenv(f"{prefix}_PRODUCT_ID", ""),
            environment_id=os.getenv(f"{prefix}_ENVIRONMENT_ID", ""),
            organization_id=os.getenv(f"{prefix}_ORGANIZATION_ID", ""),
            environment_id_dev=os.getenv(f"{prefix}_ENVIRONMENT_ID_DEV", ""),
            environment_name=environment,
            enabled=os.getenv(f"{prefix}_ENABLED", "true").lower() == "true",
            batch_mode=os.getenv(f"{prefix}_BATCH_MODE", "true").lower() == "true",
            batch_size=int(os.getenv(f"{prefix}_BATCH_SIZE", "50")),
            batch_interval=int(os.getenv(f"{prefix}_BATCH_INTERVAL", "2")),
            timeout=int(os.getenv(f"{prefix}_TIMEOUT", "10")),
            max_retries=int(os.getenv(f"{prefix}_MAX_RETRIES", "2")),
            queue_max_size=int(os.getenv(f"{prefix}_QUEUE_MAX_SIZE", "1000")),
            # Aceita a var prefixada (padrao do ecossistema) ou a raiz
            # PRODUCT_SLUG, por compat com consumidores legados.
            product_slug=(
                os.getenv(f"{prefix}_PRODUCT_SLUG", "")
                or os.getenv("PRODUCT_SLUG", "")
            ),
            log_min_level=os.getenv(f"{prefix}_LOG_MIN_LEVEL", "WARNING").upper(),
        )
    
    def is_valid(self) -> bool:
        """Verifica se a configuração é válida.

        A partir da migration 0022 do AdminCenter, `product_id`,
        `environment_id` e `organization_id` podem vir do JWT emitido em
        /auth/gerar-token/api-key (quando a chave for escopada). Por isso,
        nao sao mais obrigatorios no .env — sao preenchidos automaticamente
        em `_get_access_token`. Aqui exigimos so URL e api_key.
        """
        if not self.enabled:
            return True
        return all([self.api_url, self.api_key])

    def has_logging_identity(self) -> bool:
        """True se a config carrega os ids necessarios para escrever
        `token_usage` e logs estruturados: `product_id` + `environment_id`.

        Distinto de `is_valid()`, que so exige URL e api_key. Um produto pode
        estar CONECTADO ao AdminCenter e ainda assim nao ter identidade para
        logar — os ids chegam pelo JWT da api-key escopada, e sem eles a
        escrita seria rejeitada do outro lado.
        """
        return bool(self.product_id and self.environment_id)


class AdminCenterEndpoints:
    """Endpoints do Admin Center API"""
    TOKEN_USAGE = "/token-usage/"
    LOG_EXECUTION = "/logs/execution"
    LOG_APPLICATION = "/logs/application"
    LOG_PROCESS = "/logs/process"
    #: Passos de dentro de uma execucao (migration 0044). Recebe LISTA — o
    #: batch worker agrupa os itens da fila num POST so'.
    LOG_STEP = "/logs/step"
    SECRET_DECRYPT = "/secret/decrypt"
    ENVIRONMENT_VARIABLES = "/environment/{}/variables"
    AI_MODEL_BY_NAME = "/ai-model/consulta_nome"
    AUTH_TOKEN = "/auth/gerar-token/api-key"
    # Prompts
    PROMPT_BY_SLUG = "/prompt/{}"
    PROMPT_BY_ID = "/prompt/consulta_id"
    PROMPTS_LIST = "/prompt/listar"
    PROMPT_LOG_USAGE = "/prompt/{}/log-usage"
    # Agents
    EFFECTIVE_PROMPT = "/product/{}/agent/{}/effective-prompt"
    # Database Connections (broker central)
    DATABASE_CONNECTION_RESOLVE = "/database-connection/resolve"
    # Descoberta de identidade a partir do product_slug (ver
    # `_discover_environment_id_via_api`).
    PRODUCT_BY_SLUG = "/product/consulta_slug"
    PRODUCT_ENVIRONMENTS = "/product/{}/environment"
    # RBAC por perfil (role_agents / role_connections, migration 0037).
    # Autenticam com o JWT do USUARIO, nao com a api-key do produto: quem
    # resolve o RBAC e a sessao de quem esta perguntando.
    AGENTS_ALLOWED = "/agent/allowed"
    CONNECTIONS_ALLOWED = "/database-connection/allowed"


#: Correlacao e contador de passos da execucao em curso NA THREAD ATUAL.
#: Mesmo padrao do `_module_ctx_local` de `jobs.py`, e pela mesma razao: dois
#: pipelines em paralelo no mesmo processo nao podem compartilhar numeracao.
_step_local = local()


class _StepHandle:
    """O que `agent_step` entrega ao bloco `with`.

    Guarda o que so' se sabe DURANTE a etapa (tokens gastos, um erro tratado)
    para a linha final carregar. Progresso intermediario e' opcional e custa uma
    linha cada — util em etapa longa, desperdicio em etapa de 200ms.
    """

    def __init__(self, service, label, kind, seq, agent_id, area_agent_id,
                 connection_id, model_name, detail):
        self._service = service
        self._label = label
        self._kind = kind
        self._seq = seq
        self._agent_id = agent_id
        self._area_agent_id = area_agent_id
        self._connection_id = connection_id
        self._model_name = model_name
        self._detail = detail
        self._status = 'ok'
        self._erro = None
        self._tokens_in = None
        self._tokens_out = None

    def progress(self, percent: int, label: str = None):
        """Reporta andamento. Grava uma linha `running` — use em etapa longa."""
        self._service.log_step(label or self._label, kind=self._kind,
                               status='running', seq=self._seq,
                               agent_id=self._agent_id,
                               area_agent_id=self._area_agent_id,
                               connection_id=self._connection_id,
                               model_name=self._model_name, percent=percent)

    def note(self, label: str, kind: str = 'decision', detail: Dict = None):
        """Registra algo que aconteceu DENTRO da etapa e merece linha propria —
        uma decisao, um desvio, uma rodada de reparo."""
        self._service.log_step(label, kind=kind, status='ok',
                               agent_id=self._agent_id,
                               area_agent_id=self._area_agent_id,
                               connection_id=self._connection_id,
                               model_name=self._model_name, detail=detail)

    def done(self, tokens_in: int = None, tokens_out: int = None,
             detail: Dict = None):
        """Credita o consumo da etapa. A linha final so' sai na saida do bloco."""
        self._tokens_in = tokens_in
        self._tokens_out = tokens_out
        if detail:
            self._detail = {**(self._detail or {}), **detail}

    def fail(self, message: str):
        """Marca a etapa como falha SEM levantar excecao — para o caso em que o
        produto trata o erro e segue."""
        self._status = 'failed'
        self._erro = message

    def _encerrar(self, inicio: float, status: str, erro: str = None):
        self._service.log_step(
            self._label, kind=self._kind, status=status, seq=self._seq,
            agent_id=self._agent_id, area_agent_id=self._area_agent_id,
            connection_id=self._connection_id, model_name=self._model_name,
            percent=100 if status == 'ok' else None,
            tokens_in=self._tokens_in, tokens_out=self._tokens_out,
            duration_ms=int((time.time() - inicio) * 1000),
            error_message=erro, detail=self._detail
        )


class AdminCenterService:
    """
    Serviço genérico para integração com Admin Center API
    OTIMIZADO PARA ALTA PERFORMANCE ASSÍNCRONA
    """
    
    def __init__(self, config: AdminCenterConfig = None):
        self.config = config or AdminCenterConfig.from_env()
        self.logger = logging.getLogger(self.__class__.__name__)
        self.access_token = None
        self._session = None

        # Metadados da API key (preenchidos por `_get_access_token`). Deixam o
        # produto consumidor consultar mode/scopes/prefixo sem reabrir o JWT.
        self.api_key_mode = None
        self.api_key_scopes = []
        self.api_key_prefix = None
        self.api_key_organization_id = None

        # Sistema de batch assíncrono OTIMIZADO
        self._queue = Queue(maxsize=self.config.queue_max_size)
        self._batch_lock = Lock()
        self._worker_thread = None
        self._shutdown = False

        # Circuit breaker de autenticação. Quando renovar o token não resolve
        # o 401 (ex.: SECRET_KEY divergente entre réplicas do admincenter-api),
        # paramos de enviar por um tempo para não martelar o servidor em loop.
        self._auth_cooldown_until = 0.0
        self._auth_fail_streak = 0

        # Resolver environment_id baseado no ambiente
        self.environment_id = self._resolve_environment_id()
        self.environment_name = self.config.environment_name
        
        self._model_cache = {}
        # Cache do modelo efetivo por agente (agents.model_id resolvido via
        # effective-prompt). Chave: "<product_id>:<agent_slug>".
        self._effective_model_cache = {}
        self._cache_lock = Lock()

        # Resolver de conexoes de banco (lazy — instanciado no primeiro uso)
        self._connection_resolver = None
        self._connection_resolver_lock = Lock()

        # RBAC por perfil. Cache curto por TOKEN: a resposta depende de quem
        # pergunta, entao a chave nao pode ser global. 60s e o suficiente para
        # absorver a rajada de uma tela montando seletor sem segurar uma
        # revogacao de acesso por muito tempo.
        self._allowed_agents_cache = {}
        self._allowed_agents_lock = Lock()
        self._allowed_conns_cache = {}
        self._allowed_conns_lock = Lock()
        self._allowed_rbac_ttl = 60

        if self.config.enabled and self.config.is_valid():
            self._initialize()
        elif self.config.enabled:
            self.logger.error("Admin Center config inválida. Serviço desabilitado.")
            self.config.enabled = False
        else:
            self.logger.info("Admin Center Service desabilitado por configuração")
    
    def _resolve_environment_id(self) -> str:
        """Resolve environment_id baseado no ambiente atual"""
        if (self.config.environment_name == 'development' and 
            self.config.environment_id_dev and 
            self.config.environment_id_dev.strip()):
            return self.config.environment_id_dev
        return self.config.environment_id
    
    def _initialize(self):
        """Inicializa o serviço"""
        try:
            self._get_access_token()
            self._setup_session()

            # Quando o .env nao fixa ADMIN_CENTER_ENVIRONMENT_ID/PRODUCT_ID e a
            # api-key nao e' escopada, esses ids ficam vazios. Sem eles:
            # `get_variable()` chama /environment/None/variables -> 404, e
            # `has_logging_identity()` fica False -> token_usage e logs sao
            # descartados EM SILENCIO. slug + api-key bastam para resolver.
            if self.config.product_slug and (
                not self.environment_id or not (self.config.product_id or "").strip()
            ):
                self._discover_environment_id_via_api()

            # SEMPRE iniciar worker assíncrono para máxima performance
            self._start_batch_worker()
            
            self.logger.info(f"Admin Center Service inicializado (ASYNC MODE) - Produto: {self.config.product_id}")
            
        except Exception as e:
            self.logger.error(f"Erro ao inicializar Admin Center Service: {e}")
            self.config.enabled = False
    
    def _discover_environment_id_via_api(self) -> str:
        """Descobre `environment_id` (e `product_id`) via product_slug + ambiente.

        Usa `/product/consulta_slug` e `/product/{id}/environment`,
        autenticando com o JWT da api-key. Espelha no `config` os dois ids —
        `has_logging_identity()` le do CONFIG, e sem espelhar o guard ficaria
        False mesmo com o ambiente ja' descoberto. Retorna '' em falha (o
        produto cai para o fluxo do .env).
        """
        org = self.api_key_organization_id or self.config.organization_id
        slug = self.config.product_slug
        env_name = self.environment_name or self.config.environment_name
        if not slug or not org:
            self.logger.warning(
                "Nao da pra descobrir environment_id (slug=%s, org=%s)", slug, org
            )
            return ""
        try:
            resp = self._make_request(
                "GET",
                AdminCenterEndpoints.PRODUCT_BY_SLUG,
                params={"slug": slug, "organization_id": org},
            )
            product = (resp or {}).get("data") or {}
            product_id = product.get("id") if isinstance(product, dict) else None
            if not product_id:
                self.logger.warning(
                    "Produto slug='%s' nao encontrado para org=%s", slug, org
                )
                return ""

            if not (self.config.product_id or "").strip():
                self.config.product_id = product_id
                self.logger.info(
                    "product_id resolvido via slug='%s': %s", slug, product_id
                )

            resp = self._make_request(
                "GET",
                AdminCenterEndpoints.PRODUCT_ENVIRONMENTS.format(product_id),
                params={"name": env_name},
            )
            envs = (resp or {}).get("data") or []
            if isinstance(envs, dict):
                envs = [envs]
            for env in envs:
                if env.get("name") == env_name:
                    eid = env.get("id")
                    if eid:
                        self.environment_id = eid
                        if not (self.config.environment_id or "").strip():
                            self.config.environment_id = eid
                        self.logger.info(
                            "environment_id descoberto (slug='%s', ambiente='%s'): %s",
                            slug, env_name, eid,
                        )
                        return eid
            self.logger.warning(
                "Nenhum ambiente '%s' para product_id=%s", env_name, product_id
            )
        except Exception as e:
            self.logger.warning("Falha ao descobrir environment_id: %s", e)
        return ""

    def _get_access_token(self):
        """Obtém token de acesso usando API key.

        A resposta do AdminCenter (migration 0022) carrega
        organization_id/product_id/environment_id derivados do escopo da
        chave. Quando esses campos vem populados, preenchem o config — entao
        o produto consumidor nao precisa mais defini-los no .env. Valores
        vindos do .env continuam vencendo (compat com produtos legados que
        querem forcar um escopo diferente).
        """
        url = f"{self.config.api_url}{AdminCenterEndpoints.AUTH_TOKEN}"
        headers = {'api-key': self.config.api_key}

        try:
            response = requests.post(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()

            data = response.json()
            payload = data.get('data') or {}
            self.access_token = payload['access_token']

            # Auto-preenche escopo a partir do JWT response quando nao
            # houver valor explicito no .env. Vide migration 0022 e
            # /auth/gerar-token/api-key.
            # Metadados da chave (api_keys). Chave legada hardcoded nao os
            # traz — toleramos a ausencia.
            self.api_key_mode = payload.get('mode')  # 'test' | 'live' | None
            self.api_key_scopes = payload.get('scopes') or []
            self.api_key_prefix = payload.get('api_key_prefix')
            self.api_key_organization_id = payload.get('organization_id')

            if not self.config.organization_id and payload.get('organization_id'):
                self.config.organization_id = payload['organization_id']
            if not self.config.product_id and payload.get('product_id'):
                self.config.product_id = payload['product_id']
            if not self.config.environment_id and payload.get('environment_id'):
                self.config.environment_id = payload['environment_id']
                # `environment_id` resolvido pelo helper precisa refletir o novo valor.
                self.environment_id = self._resolve_environment_id()

            self.logger.info(
                "Token de acesso obtido com sucesso "
                f"(org={self.config.organization_id}, product={self.config.product_id}, "
                f"env={self.environment_id})"
            )

        except Exception as e:
            self.logger.error(f"Erro ao obter token: {e}")
            raise
    
    def _setup_session(self):
        """Configura sessão HTTP reutilizável.

        O JWT emitido por /auth/gerar-token/api-key ja carrega o claim `mode`
        derivado do prefixo da chave (sk_live_* ou sk_test_*), entao o backend
        resolve o silo automaticamente. Mesmo assim, propagamos o header
        X-AdminCenter-Mode como fallback explicito — util para debug e quando
        o produto quer forcar um modo (via env ADMIN_CENTER_MODE).
        """
        self._session = requests.Session()
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }
        mode = self._resolve_mode_header()
        if mode:
            headers["X-AdminCenter-Mode"] = mode
        self._session.headers.update(headers)

    def _resolve_mode_header(self) -> Optional[str]:
        """Determina o modo (test|live) a propagar nas requests.
        Ordem: env explicita > prefixo da api_key > None (deixa o backend
        deduzir do JWT)."""
        explicit = os.getenv("ADMIN_CENTER_MODE", "").strip().lower()
        if explicit in ("test", "live"):
            return explicit
        api_key = self.config.api_key or ""
        if api_key.startswith("sk_test_"):
            return "test"
        if api_key.startswith("sk_live_"):
            return "live"
        return None
    
    def _make_request(self, method: str, endpoint: str, data: Any = None,
                     params: Dict = None, retry_count: int = 0) -> Optional[Dict]:
        """Executa requisição HTTP com retry automático.

        `data` e' normalmente um dict, mas `/logs/step` recebe uma LISTA de
        passos num POST so' — dai o `Any`.
        """
        # Circuit breaker: após falhas de auth persistentes, descartamos o envio
        # imediatamente (sem tocar a rede) até o cooldown expirar. Evita o loop
        # de auth+retry martelando o AdminCenter com requests fadadas a 401.
        if time.time() < self._auth_cooldown_until:
            return None

        url = f"{self.config.api_url}{endpoint}"

        try:
            if data and self.logger.isEnabledFor(logging.DEBUG):
                self.logger.debug(f"Enviando para {method} {endpoint}: {json.dumps(data, indent=2, default=str)}")
            
            response = self._session.request(
                method=method,
                url=url,
                json=data,
                params=params,
                timeout=self.config.timeout
            )
            
            self.logger.debug(f"Resposta {response.status_code} de {endpoint}")
            
            if response.status_code in [200, 201]:
                # Sucesso: zera o circuit breaker de auth.
                self._auth_fail_streak = 0
                self._auth_cooldown_until = 0.0
                return response.json()
            elif response.status_code == 422:
                try:
                    error_detail = response.json()
                    self.logger.error(f"Erro de validação 422 em {endpoint}: {json.dumps(error_detail, indent=2)}")
                except:
                    self.logger.error(f"Erro de validação 422 em {endpoint}: {response.text}")
                return None
            elif response.status_code == 401:
                # 401 pode ser token expirado (renovar resolve) OU assinatura
                # inválida — ex.: SECRET_KEY divergente entre réplicas do
                # admincenter-api, em que renovar NÃO adianta. Renovamos no
                # máximo 1x; se persistir, abrimos o circuit breaker em vez de
                # ficar em loop de auth+retry.
                if retry_count < 1:
                    self.logger.warning("401 recebido, renovando token e tentando novamente...")
                    self._get_access_token()
                    self._setup_session()
                    return self._make_request(method, endpoint, data, params, retry_count + 1)
                # Renovou e continuou 401 → falha persistente.
                self._open_auth_circuit()
                return None
            elif response.status_code >= 500 and retry_count < self.config.max_retries:
                self.logger.warning(f"Erro servidor {response.status_code}, tentativa {retry_count + 1}")
                time.sleep(2 ** retry_count)
                return self._make_request(method, endpoint, data, params, retry_count + 1)
            else:
                self.logger.warning(f"Erro HTTP {response.status_code}: {response.text}")
                return None
                
        except json.JSONDecodeError as e:
            self.logger.error(f"Erro de JSON no payload: {e}")
            return None
        except requests.RequestException as e:
            if retry_count < self.config.max_retries:
                self.logger.warning(f"Erro de conexão, tentativa {retry_count + 1}: {e}")
                time.sleep(2 ** retry_count)
                return self._make_request(method, endpoint, data, params, retry_count + 1)
            
            self.logger.warning(f"Erro de conexão final: {e}")
            return None
    
    def _open_auth_circuit(self):
        """Abre o circuit breaker de autenticação com backoff exponencial.

        Acionado quando renovar o token não resolve o 401 (assinatura inválida).
        Causa típica: `SECRET_KEY` diferente entre réplicas do admincenter-api,
        em que o pod que assina o token não é o mesmo que valida. Enquanto o
        circuit estiver aberto, `_make_request` descarta os envios sem tocar a
        rede, evitando o flood de auth+retry. O backoff cresce a cada janela de
        falha (30s, 60s, 120s ... teto de 5 min) e zera no primeiro sucesso.
        """
        self._auth_fail_streak += 1
        cooldown = min(30 * (2 ** (self._auth_fail_streak - 1)), 300)
        self._auth_cooldown_until = time.time() + cooldown
        # Loga só na abertura e a cada 10 janelas, para não virar ruído próprio.
        if self._auth_fail_streak == 1 or self._auth_fail_streak % 10 == 0:
            self.logger.error(
                "AdminCenter: 401 persistente após renovar token — pausando envios por "
                f"{cooldown}s (falha #{self._auth_fail_streak}). Verifique se SECRET_KEY "
                "é idêntica em todas as réplicas do admincenter-api."
            )

    def _validate_token_usage_payload(self, payload: Dict) -> bool:
        """Valida payload de token usage antes do envio"""
        required_fields = ["product_id", "environment_id", "model_id", 
                          "prompt_tokens", "completion_tokens"]
        
        for field in required_fields:
            if field not in payload or payload[field] is None:
                self.logger.error(f"Campo obrigatório ausente: {field}")
                return False
        
        if not isinstance(payload["prompt_tokens"], int) or payload["prompt_tokens"] < 0:
            self.logger.error("prompt_tokens deve ser inteiro não-negativo")
            return False
            
        if not isinstance(payload["completion_tokens"], int) or payload["completion_tokens"] < 0:
            self.logger.error("completion_tokens deve ser inteiro não-negativo")
            return False
            
        return True
    
    def _enqueue_safely(self, item_type: str, payload: Dict) -> bool:
        """
        Adiciona item na fila de forma segura, SEM JAMAIS BLOQUEAR A APLICAÇÃO
        """
        try:
            # Tentar adicionar na fila sem bloquear
            self._queue.put_nowait((item_type, payload))
            return True
        except:
            # Fila cheia - descartar item mais antigo e continuar
            try:
                self._queue.get_nowait()  # Remove item mais antigo
                self._queue.put_nowait((item_type, payload))  # Adiciona novo
                self.logger.warning(f"Fila Admin Center cheia. Item antigo descartado para {item_type}")
                return True
            except:
                # Se mesmo assim falhar, apenas log e continua aplicação
                self.logger.error(f"Não foi possível enfileirar {item_type}. Aplicação continua normalmente.")
                return False
    
    # ==================== API METHODS ====================
    
    def get_variable(self, environment_id: str = None) -> Optional[str]:
        """
        Busca variável de ambiente do Admin Center
        
        Args:
            environment_id: ID do ambiente (usa padrão se não informado)
        
        Returns:
            Valor da variável ou None se não encontrada
        """
        if not self.config.enabled:
            return None
        
        env_id = environment_id or self.environment_id
        if not env_id and self.config.product_slug:
            env_id = self._discover_environment_id_via_api()
        if not env_id:
            # Sem este guard o endpoint virava /environment/None/variables e o
            # produto subia sem nenhuma variavel, com um 404 solto no log.
            self.logger.warning(
                "get_variable: environment_id indisponivel "
                "(.env sem ADMIN_CENTER_ENVIRONMENT_ID* e descoberta via slug falhou)"
            )
            return None

        params = {'include_values': True, 'environment_name': self.environment_name}

        endpoint = AdminCenterEndpoints.ENVIRONMENT_VARIABLES.format(env_id)
        response = self._make_request("GET", endpoint, params=params)
        
        if response and "data" in response:
            return response["data"]
        
        self.logger.debug(f"Variável '{environment_id}' não encontrada")
        return None
    
    def get_secret(self, secret_name: str) -> Optional[str]:
        """
        Busca e descriptografa um secret do Admin Center
        
        Args:
            secret_name: Nome do secret
        
        Returns:
            Valor descriptografado ou None se não encontrado
        """
        if not self.config.enabled:
            return None
        
        params = {
            "secret_id": secret_name,
            "organization_id": self.config.organization_id or self.config.product_id
        }
        
        response = self._make_request("GET", AdminCenterEndpoints.SECRET_DECRYPT, params=params)

        if response and "data" in response:
            return response["data"].get("decrypted_value")

        self.logger.debug(f"Secret '{secret_name}' não encontrado")
        return None

    # ====================================================
    # DATABASE CONNECTIONS — broker centralizado
    # ====================================================

    def _get_connection_resolver(self):
        """Lazy: cria `ConnectionResolver` na primeira chamada."""
        if self._connection_resolver is None:
            with self._connection_resolver_lock:
                if self._connection_resolver is None:
                    # Import local — evita custo no startup quando o produto
                    # nao usa conexoes de banco do AdminCenter.
                    from .connections import ConnectionResolver
                    self._connection_resolver = ConnectionResolver(self)
        return self._connection_resolver

    def resolve_connection(
        self,
        alias: Optional[str] = None,
        connection_id: Optional[str] = None,
        force_refresh: bool = False,
    ):
        """Resolve credenciais decriptadas de uma conexao cadastrada no
        AdminCenter.

        Args:
            alias: identificador da conexao (slug). Ex: 'casan_prod'.
            connection_id: UUID da conexao (alternativo ao alias).
            force_refresh: ignora cache em memoria.

        Returns:
            `ResolvedConnection` ou None se nao encontrada / sem permissao.
        """
        if not self.config.enabled:
            return None
        return self._get_connection_resolver().resolve(
            alias=alias,
            connection_id=connection_id,
            force_refresh=force_refresh,
        )

    def get_db_connection(self, alias: str, **psycopg2_kwargs):
        """Abre conexao psycopg2 para o alias informado.

        Cuida de tunel SSH/Cloudflare automaticamente quando a conexao
        cadastrada usa tunel.

        Returns:
            `psycopg2.extensions.connection`. Lembre de fechar (`with` block
            ou `conn.close()`).
        """
        return self._get_connection_resolver().get_psycopg2(alias, **psycopg2_kwargs)

    def get_db_engine(self, alias: str, **engine_kwargs):
        """Cria SQLAlchemy `Engine` para o alias. Descarte com `.dispose()`."""
        return self._get_connection_resolver().get_engine(alias, **engine_kwargs)

    def get_bigquery_client(self, alias: str, maximum_bytes_billed: Optional[int] = None):
        """`google.cloud.bigquery.Client` de uma conexao engine='bigquery'.

        Teto de custo por consulta: `maximum_bytes_billed` ou env
        `BIGQUERY_MAXIMUM_BYTES_BILLED`. Requer o extra `[bigquery]`.
        """
        return self._get_connection_resolver().get_bigquery_client(
            alias, maximum_bytes_billed=maximum_bytes_billed
        )

    def get_db_session(self, alias: str, **engine_kwargs):
        """Context manager: abre `Engine` + `Session`, commita no exit ou
        rollback em caso de excecao, depois fecha tudo.

        Uso::

            with admin.get_db_session('casan_prod') as session:
                session.execute(text('SELECT 1'))
        """
        return self._get_connection_resolver().get_session(alias, **engine_kwargs)

    def invalidate_connection_cache(self, alias: Optional[str] = None) -> None:
        """Limpa o cache de conexoes resolvidas. Sem alias, limpa tudo."""
        if self._connection_resolver is not None:
            self._connection_resolver.invalidate(alias)

    def track_token_usage(self, model_name: str = None, prompt_tokens: int = 0,
                         completion_tokens: int = 0, request_id: str = None,
                         user_id: str = None, endpoint_called: str = None,
                         prompt_id: str = None, metadata: Dict = {},
                         agent_slug: str = None, agent_id: str = None,
                         area_agent_slug: str = None,
                         area_agent_id: str = None) -> bool:
        """
        Registra uso de tokens de IA - SEMPRE ASSÍNCRONO para máxima performance

        Duas dimensões de custo por agente (colunas da migration 0036):

        - **ETAPA executora** (`agent_id`): quem fez ESTA chamada de LLM —
          roteador de tabelas, gerador de SQL, analista, validador.
        - **ÁREA de negócio** (`area_agent_id`): quem é o dono da requisição
          inteira. Uma pergunta no chat dispara várias chamadas de LLM, todas
          da mesma área; a área é marcada uma vez na entrada do endpoint com
          `definir_agente()` e herdada por todo o tracking do contexto.

        Sem etapa explícita, a etapa É a própria área — assim não se perde
        atribuição em quem só marcou o agente na entrada.

        Args:
            model_name: nome do modelo LLM usado (ex.: 'gpt-4o'). Se omitido e
                        `agent_slug` for informado, cai no modelo configurado no
                        agente (agents.model_id via effective-prompt) — torna a
                        config do agente autoritativa de ponta a ponta.
            agent_slug: slug do agente ETAPA. Resolve o modelo padrão quando
                        `model_name` não é passado E resolve `agent_id`.
            area_agent_slug: slug do agente ÁREA. Resolve `area_agent_id`.
            agent_id / area_agent_id: UUIDs de `agents.id`, quando o produto já
                        os conhece. Vencem os slugs correspondentes. Omitidos
                        do payload quando não resolvem — as colunas são FK para
                        `agents.id`, então id inventado derrubaria a escrita.
            prompt_id: ID do prompt cadastrado no AdminCenter (opcional).
                       Permite analytics de uso por prompt.
        """
        if not self.config.enabled:
            return False
        if not self.config.has_logging_identity():
            self.logger.debug(
                "record_token_usage pulado: product_id/environment_id nao configurados"
            )
            return False

        # Resolução do modelo: nome explícito do produto tem prioridade; senão,
        # cai no modelo configurado no agente (effective-prompt, cacheado).
        model_id = None
        resolved_name = model_name
        if model_name:
            model_id = self._get_model_id_by_name(model_name)
        elif agent_slug:
            model_id, resolved_name = self._resolve_agent_model(agent_slug)

        if not model_id:
            self.logger.warning(
                f"Model ID não resolvido (model_name='{model_name}', agent_slug='{agent_slug}'). "
                "Pulando registro."
            )
            return False

        if not request_id:
            request_id = str(uuid.uuid4())

        # Resolução das dimensões de agente. O id explícito do call site vence;
        # senão traduz o slug. Sem etapa explícita, a etapa é a própria área.
        etapa_slug = (agent_slug or '').strip() or (area_agent_slug or '').strip()
        if not agent_id and etapa_slug:
            agent_id = self.resolve_agent_id(etapa_slug)
        if not area_agent_id and (area_agent_slug or '').strip():
            area_agent_id = self.resolve_agent_id(area_agent_slug.strip())

        payload = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
            "model_id": model_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "alert_metadata": {
                "endpoint": endpoint_called or "/unknown",
                "model_name": resolved_name,
                # Slugs seguem no metadata mesmo com as colunas preenchidas: é
                # o que permite ler o rastro quando o agente não está vinculado
                # ao produto e o id não resolveu.
                **({"agent_slug": etapa_slug} if etapa_slug else {}),
                **({"area_agent_slug": area_agent_slug} if area_agent_slug else {}),
                **({"prompt_id": prompt_id} if prompt_id else {}),
                **metadata
            }
        }

        if request_id:
            payload["request_id"] = request_id
        if user_id:
            payload["user_id"] = user_id
        if prompt_id:
            payload["prompt_id"] = prompt_id
        # Dimensões de agente (migration 0036): etapa executora + área de
        # negócio. Omitidas quando None para retrocompat com backends antigos.
        if agent_id:
            payload["agent_id"] = agent_id
        if area_agent_id:
            payload["area_agent_id"] = area_agent_id

        if not self._validate_token_usage_payload(payload):
            return False
        
        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("token_usage", payload)
        
    def _get_model_id_by_name(self, model_name: str) -> Optional[str]:
        """
        Busca model_id com cache inteligente
        """
        with self._cache_lock:
            cached_data = self._model_cache.get(model_name)
            
            if cached_data and cached_data.get("name") == model_name:
                self.logger.debug(f"Cache hit para modelo '{model_name}': {cached_data['id']}")
                return cached_data["id"]
            
            self.logger.debug(f"Cache miss para modelo '{model_name}', buscando na API...")
            model_id = self._fetch_model_id_from_api(model_name)
            
            if model_id:
                self._model_cache[model_name] = {
                    "id": model_id,
                    "name": model_name
                }
                self.logger.debug(f"Cache atualizado: {model_name} -> {model_id}")
                return model_id
            
            return None
    
    def _fetch_model_id_from_api(self, model_name: str) -> Optional[str]:
        """
        Busca o model_id real na API
        """
        try:
            params = {"name": model_name}
            response = self._make_request("GET", AdminCenterEndpoints.AI_MODEL_BY_NAME, params=params)
            
            if response and "data" in response:
                models = response["data"]
                if models and len(models) > 0:
                    return str(models["id"])
            
            self.logger.warning(f"Modelo '{model_name}' não encontrado na API")
            return None
            
        except Exception as e:
            self.logger.error(f"Erro ao buscar model_id para '{model_name}': {e}")
            return None
    
    def invalidate_model_cache(self, model_name: str = None):
        """
        Invalida cache de modelo específico ou todo o cache
        """
        with self._cache_lock:
            if model_name:
                self._model_cache.pop(model_name, None)
                self.logger.debug(f"Cache invalidado para modelo: {model_name}")
            else:
                self._model_cache.clear()
                self._effective_model_cache.clear()
                self.logger.debug("Todo cache de modelos invalidado")

    # Janela em que um slug que nao resolveu para de ser reperguntado.
    _NEGATIVE_TTL = 300

    def _resolve_agent_info(self, agent_slug: str, product_id: str = None,
                            exigir_modelo: bool = False) -> Dict:
        """Resolve, numa unica chamada ao effective-prompt, tudo que o tracking
        precisa saber sobre um agente: `agent_id` (UUID de `agents.id`) e o
        modelo EFETIVO (override do produto -> padrao do agente).

        Devolve `{'agent_id', 'model_id', 'model_name'}` — chaves ausentes/None
        quando o agente nao esta vinculado ao produto ou nao tem modelo.

        Cache por (product_id, agent_slug). O `agent_id` e' identidade estavel,
        entao e' cacheado SEMPRE; o modelo so' quando resolvido, para
        reautotentar assim que alguem o configurar no painel. Sem essa
        distincao, agente sem modelo nunca cacheava nada e cada chamada de LLM
        pagava um roundtrip so' para descobrir o `agent_id`.

        A NEGATIVA tambem e' cacheada, por `_NEGATIVE_TTL` segundos. Este
        metodo fica no caminho quente de `track_token_usage` — sem cachear a
        negativa, um slug que nao resolve (agente nao vinculado ao produto,
        typo no call site, AdminCenter oscilando) somaria um roundtrip HTTP a
        CADA chamada de LLM, indefinidamente. Telemetria nao pode custar mais
        que o trabalho que ela mede. O TTL e' o que faz o vinculo recem-criado
        no painel passar a valer sozinho.
        """
        pid = product_id or self.config.product_id
        cache_key = f"{pid}:{agent_slug}"
        agora = time.time()

        with self._cache_lock:
            cached = self._effective_model_cache.get(cache_key)

        if cached is not None:
            negativo_ate = cached.get("_negativo_ate")
            if negativo_ate is not None:
                if agora < negativo_ate:
                    return {}
            # Entrada com modelo pendente e' meio-cache: serve o agent_id de
            # graca, mas so' quem QUER o modelo (`exigir_modelo`) paga o
            # refetch. Sem essa distincao, resolver o agent_id de um agente
            # sem modelo configurado batia na rede a cada chamada de LLM.
            elif not (exigir_modelo and cached.get("_model_pendente")):
                return cached

        ep = self.get_effective_prompt(agent_slug, product_id)
        if not ep:
            with self._cache_lock:
                self._effective_model_cache[cache_key] = {
                    "_negativo_ate": agora + self._NEGATIVE_TTL
                }
            return {}

        model_id = ep.get("model_id")
        info = {
            "agent_id": ep.get("agent_id"),
            "model_id": model_id,
            "model_name": ep.get("model_name") or ep.get("model_display_name"),
        }
        if not model_id:
            info["_model_pendente"] = True

        with self._cache_lock:
            self._effective_model_cache[cache_key] = info
        return info

    def _resolve_agent_model(self, agent_slug: str, product_id: str = None):
        """Resolve (model_id, model_name) do modelo EFETIVO do agente.

        Retorna (None, None) quando o agente não tem modelo configurado.
        """
        info = self._resolve_agent_info(agent_slug, product_id, exigir_modelo=True)
        return info.get("model_id"), info.get("model_name")

    def resolve_agent_id(self, agent_slug: str, product_id: str = None) -> Optional[str]:
        """UUID de `agents.id` para o slug, ou None se nao resolver.

        `token_usage.agent_id`/`area_agent_id` sao FK para `agents.id` — mandar
        um id inventado derruba a escrita do lote inteiro do outro lado. Por
        isso o None aqui significa "omita a dimensao", nunca "mande assim
        mesmo".

        Resolve via `effective-prompt`, entao so' enxerga agente VINCULADO ao
        produto (`product_agents`). Agente existente mas nao vinculado devolve
        None — o vinculo e' o que da sentido a atribuicao de custo por produto.
        """
        if not agent_slug or not self.config.enabled:
            return None
        return self._resolve_agent_info(agent_slug, product_id).get("agent_id")

    def invalidate_effective_model_cache(self, agent_slug: str = None, product_id: str = None):
        """Invalida o cache do modelo efetivo do agente (chamar após mudar o
        modelo do agente no AdminCenter)."""
        with self._cache_lock:
            if agent_slug:
                pid = product_id or self.config.product_id
                self._effective_model_cache.pop(f"{pid}:{agent_slug}", None)
            else:
                self._effective_model_cache.clear()

    # Ordem dos níveis, para comparar com `config.log_min_level`.
    _ORDEM_NIVEL = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30,
                    "ERROR": 40, "CRITICAL": 50, "FATAL": 50}

    def _nivel_persistivel(self, level: str) -> bool:
        """True se este nível deve ir para a tabela de logs do AdminCenter.

        Nível desconhecido PASSA: melhor gravar um log a mais do que engolir em
        silêncio algo que ninguém previu — o custo do erro é assimétrico.
        """
        minimo = self._ORDEM_NIVEL.get((self.config.log_min_level or "").upper())
        if minimo is None:
            return True
        atual = self._ORDEM_NIVEL.get((level or "").upper())
        if atual is None:
            return True
        return atual >= minimo

    # Chaves de `context` que a tabela `application_logs` tem coluna dedicada
    # para receber. Os produtores montam o dict com os nomes do `logging` do
    # Python (`logger`/`funcName`/`lineno`) ou com apelidos curtos
    # (`function`/`line`, que e' o que o interceptor do talk-api usa); o
    # contrato usa `logger_name`/`function_name`/`line_number`. Todos os
    # apelidos entram aqui — chave nao mapeada cai no `extra_data`, entao a
    # ausencia de um alias nao perde o dado, so o tira da coluna consultavel.
    _CONTEXT_PARA_CAMPO = {
        "logger": "logger_name",
        "logger_name": "logger_name",
        "module": "module_name",
        "module_name": "module_name",
        "funcName": "function_name",
        "func_name": "function_name",
        "function": "function_name",
        "function_name": "function_name",
        "lineno": "line_number",
        "line": "line_number",
        "line_number": "line_number",
        "exception_type": "exception_type",
        "exception_message": "exception_message",
    }

    def log_application(self, level: str, message: str, stack_trace: str = None,
                       context: Dict = None,
                       logger_name: str = None, module_name: str = None,
                       function_name: str = None, line_number: int = None,
                       exception_type: str = None, exception_message: str = None) -> bool:
        """
        Registra log de aplicação - SEMPRE ASSÍNCRONO

        Campos top-level (logger_name, module_name, function_name, line_number,
        exception_*) batem 1-pra-1 com a tabela application_logs e ficam
        consultáveis sem ter que abrir o JSON. O resto vai pra extra_data.

        O `context` é DESMEMBRADO nesses campos: `logger`, `module`, `funcName`
        e `lineno` (nomes do `logging` do Python, que é como os produtos
        montam o dict) viram `logger_name`/`module_name`/`function_name`/
        `line_number`. Sem isso o produtor coletava tudo certo e o dado morria
        na porta de entrada — `ApplicationLogPOST` não tem campo `context`, e o
        Pydantic descarta chave desconhecida sem reclamar. Parâmetro explícito
        vence o que vier no `context`.
        """
        if not self.config.enabled:
            return False
        if not self._nivel_persistivel(level):
            # Descartado ANTES da fila: nao gasta rede, lote nem linha no banco.
            return False
        if not self.config.has_logging_identity():
            self.logger.debug(
                "log_application pulado: product_id/environment_id nao configurados"
            )
            return False

        # Quando este log e' emitido de dentro de um handler de job, injeta
        # run_id + job_slug em extra_data automaticamente. Isso permite que
        # a UI filtre `application_logs` por execucao especifica (sem
        # depender de substring na mensagem). Import lazy pra evitar
        # circular: service.py <- jobs.py <- service.py.
        merged_extra = dict(context or {})
        try:
            from .jobs import current_run_context  # noqa: PLC0415
            ctx = current_run_context()
            if ctx:
                # Nao sobrescreve se o caller ja preencheu — respeita override explicito.
                if ctx.run_id and 'run_id' not in merged_extra:
                    merged_extra['run_id'] = ctx.run_id
                if ctx.job_slug and 'job_slug' not in merged_extra:
                    merged_extra['job_slug'] = ctx.job_slug
        except Exception:
            pass

        campos = {
            "logger_name": logger_name,
            "module_name": module_name,
            "function_name": function_name,
            "line_number": line_number,
            "exception_type": exception_type,
            "exception_message": exception_message,
        }
        extra_data = {}

        for chave, valor in merged_extra.items():
            destino = self._CONTEXT_PARA_CAMPO.get(chave)
            if destino and campos.get(destino) is None:
                campos[destino] = valor
            elif destino is None:
                # Não é campo do contrato: preserva no JSONB em vez de perder
                # (é onde vivem `run_id`/`job_slug` e o que o produto mandou).
                extra_data[chave] = valor

        # `line_number` é inteiro no contrato; string quebraria o lote inteiro.
        if campos["line_number"] is not None:
            try:
                campos["line_number"] = int(campos["line_number"])
            except (TypeError, ValueError):
                extra_data["line_number"] = campos["line_number"]
                campos["line_number"] = None

        payload = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
            "log_level": level.upper(),
            "message": message,
            "stack_trace": stack_trace,
            "extra_data": extra_data,
            "timestamp": datetime.utcnow().isoformat(),
            **{k: v for k, v in campos.items() if v is not None},
        }

        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("log_application", payload)

    def get_application_logs(self,
                             log_level: Optional[str] = None,
                             logger_name: Optional[str] = None,
                             message: Optional[str] = None,
                             data_inicio: Optional[str] = None,
                             data_fim: Optional[str] = None,
                             extra_data_filter: Optional[Dict[str, Any]] = None,
                             pagina: int = 0,
                             tamanho_pagina: int = 0) -> List[Dict[str, Any]]:
        """Consulta application_logs do AdminCenter (sincrono).

        Filtros opcionais. `extra_data_filter` aceita pares chave/valor sobre o
        jsonb extra_data — match exato (ex.: {"status": "enviado",
        "mes_referencia": "Abril/2026"}). `data_inicio`/`data_fim` em
        formato 'YYYY-MM-DD'. Devolve lista de dicts; vazia quando nada bate.
        """
        if not self.config.enabled:
            return []

        params: Dict[str, Any] = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
        }
        if log_level:        params["log_level"] = log_level
        if logger_name:      params["logger_name"] = logger_name
        if message:          params["message"] = message
        if data_inicio:      params["data_inicio"] = data_inicio
        if data_fim:         params["data_fim"] = data_fim
        if pagina > 0:       params["pagina"] = pagina
        if tamanho_pagina > 0: params["tamanho_pagina"] = tamanho_pagina
        if extra_data_filter:
            params["extra_data_filter"] = ",".join(
                f"{k}={v}" for k, v in extra_data_filter.items() if v is not None
            )

        try:
            resp = self._make_request("GET", AdminCenterEndpoints.LOG_APPLICATION,
                                      params=params)
        except Exception as e:
            self.logger.warning(f"get_application_logs falhou: {e}")
            return []

        if not resp:
            return []
        data = resp.get("data") if isinstance(resp, dict) else resp
        if isinstance(data, dict) and "items" in data:
            data = data["items"]
        return data or []

    def log_execution(self, endpoint: str, method: str, status_code: int,
                     response_time_ms: int, error: str = None) -> bool:
        """
        Registra log de execução HTTP - SEMPRE ASSÍNCRONO
        """
        if not self.config.enabled:
            return False
        if not self.config.has_logging_identity():
            self.logger.debug(
                "log_execution pulado: product_id/environment_id nao configurados"
            )
            return False

        payload = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
            "endpoint": endpoint,
            "method": method,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
            "timestamp": datetime.utcnow().isoformat(),
            "error": error
        }
        
        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("log_execution", payload)
    
    def log_process(self, process_name: str, status: str, duration_ms: int = None,
               metadata: Dict = None, step_name: str = None,
               error_message: str = None, input_data: Dict = None,
               output_data: Dict = None, job_id: str = None,
               execution_id: Any = None, agent_slug: str = None,
               area_agent_slug: str = None, agent_id: str = None,
               area_agent_id: str = None, connection_id: str = None) -> bool:
        """
        Registra log de processo de negócio - SEMPRE ASSÍNCRONO

        job_id: normalmente não precisa ser informado. Quando o log é emitido
        de dentro de um handler de job (via JobRunner), o job_id é herdado
        automaticamente do run context — o que permite ao faturamento vincular
        a execução ao job (cobrança de mensagens Meta é opt-in por job).

        execution_id: correlaciona as chamadas de UMA execução (o `started` e o
        `completed`/`failed` do mesmo ciclo). Sem ele, cada chamada vira uma
        linha solta em `process_execution_logs` e o par início↔fim só pode ser
        adivinhado por (produto, process_name, proximidade no tempo) — o que
        erra justamente quando duas execuções se sobrepõem, que é quando a
        leitura do log mais importa. Vai no campo `request_id` do backend.

        Aceita `uuid.UUID` ou string, mas **precisa ser um UUID válido**: o
        contrato tipa o campo como UUID e recusaria o lote inteiro. Valor
        inválido é descartado com aviso — telemetria não pode derrubar nem
        calar o log do produto.

        agent_slug / area_agent_slug: QUEM executou (migration 0043). Resolvem
        para `agents.id` pelo mesmo `_resolve_agent_info` cacheado que o token
        tracking usa — de propósito, para que custo e execução apontem para o
        MESMO id e o JOIN entre `token_usage` e `process_execution_logs` por
        agente seja direto. Antes disso dava para saber quanto um agente gastou,
        mas não o que ele fez.

        connection_id: sobre qual base do cofre o processo rodou. Sem ele, essa
        pergunta só se responde garimpando `input_data`, quando o produto
        lembrou de gravar.
        """
        if not self.config.enabled:
            return False
        if not self.config.has_logging_identity():
            self.logger.debug(
                "log_process pulado: product_id/environment_id nao configurados"
            )
            return False

        try:
            product_uuid = UUID(self.config.product_id)
            environment_uuid = UUID(self.environment_id)
        except ValueError as e:
            self.logger.error(f"IDs inválidos: product_id={self.config.product_id}, environment_id={self.environment_id}")
            return False

        now = datetime.utcnow()

        # Herda o job_id do run context quando emitido de dentro de um handler
        # de job e o caller não passou explicitamente. Import lazy pra evitar
        # circular: service.py <- jobs.py <- service.py. Override explícito manda.
        if not job_id:
            try:
                from .jobs import current_run_context  # noqa: PLC0415
                ctx = current_run_context()
                if ctx and getattr(ctx, 'job_id', None):
                    job_id = ctx.job_id
            except Exception:
                pass

        payload = {
            "product_id": str(product_uuid),
            "environment_id": str(environment_uuid),
            "job_id": job_id,
            "process_name": process_name,
            "status": status.lower(),
            "started_at": now.isoformat() if status.lower() == "started" else None,
            "finished_at": now.isoformat() if status.lower() in ["completed", "failed"] else None,
            "duration_ms": duration_ms,
            "input_data": input_data or {},
            "output_data": output_data or {},
            "error_message": error_message,
            "retry_count": 0,
            "process_metadata": metadata or {}
        }

        if step_name:
            payload["step_name"] = step_name

        # Dimensoes de agente (0043). Slug tem precedencia menor que o id
        # explicito: quem ja' resolveu o UUID nao paga a resolucao de novo.
        if not agent_id and agent_slug:
            agent_id = self.resolve_agent_id(agent_slug)
        if not area_agent_id and (area_agent_slug or '').strip():
            area_agent_id = self.resolve_agent_id(area_agent_slug.strip())

        # UUID invalido e' DESCARTADO, nunca enviado: o campo e' tipado como
        # UUID do outro lado e um valor torto recusaria o lote inteiro.
        for chave, valor in (("agent_id", agent_id),
                             ("area_agent_id", area_agent_id),
                             ("connection_id", connection_id)):
            if not valor:
                continue
            try:
                payload[chave] = str(UUID(str(valor)))
            except (ValueError, AttributeError, TypeError):
                self.logger.warning(
                    "log_process: %s='%s' nao e' UUID valido — dimensao omitida (%s)",
                    chave, valor, process_name
                )

        if execution_id is not None:
            try:
                payload["request_id"] = str(UUID(str(execution_id)))
            except (ValueError, AttributeError, TypeError):
                self.logger.warning(
                    "execution_id '%s' nao e' UUID valido — log_process segue "
                    "sem correlacao (%s/%s)", execution_id, process_name, status
                )

        # Remover campos None para evitar problemas de serialização
        payload = {k: v for k, v in payload.items() if v is not None}
        
        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("log_process", payload)

    # ==================== EXECUTION STEPS ====================
    #
    # A linha do tempo de DENTRO de uma execucao (migration 0044).
    #
    # `log_process` grava o RUN — e e' o run que o faturamento conta em minutos.
    # Passo e' outra coisa: append-only, barato, NAO faturavel. Gravar passo como
    # run multiplicaria a fatura do cliente pelo numero de etapas do pipeline,
    # porque o relatorio decide o que cobra pelo NOME do processo
    # (`isBillable`), nao por `parent_execution_id`.

    def execution_scope(self, correlation_id: str = None):
        """Agrupa os passos de UMA execucao logica que nao vem de job.

        Dentro de um handler do `JobRunner` isso e' desnecessario: `agent_step`
        herda o `run_id` do run context, como `log_process` ja' herda o `job_id`.
        O escopo existe para o outro caso — um pipeline sincrono de request HTTP
        (o `/api/query` do Talk, a geracao do Vision), onde nao ha run.

        Reentrante: um escopo aninhado nao rouba a correlacao do de fora.

            with admin.execution_scope() as corr:
                with admin.agent_step('talk-router', label='roteando'):
                    ...
        """
        return self._execution_scope(correlation_id)

    @contextmanager
    def _execution_scope(self, correlation_id: str = None):
        anterior_corr = getattr(_step_local, 'correlation_id', None)
        anterior_seq = getattr(_step_local, 'seq', 0)
        corr = correlation_id or str(uuid.uuid4())
        _step_local.correlation_id = corr
        _step_local.seq = 0
        try:
            yield corr
        finally:
            _step_local.correlation_id = anterior_corr
            _step_local.seq = anterior_seq

    def _next_step_seq(self) -> int:
        atual = getattr(_step_local, 'seq', 0)
        _step_local.seq = atual + 1
        return atual

    def log_step(self, label: str, kind: str = 'step', status: str = 'ok',
                 agent_id: str = None, area_agent_id: str = None,
                 connection_id: str = None, run_id: str = None,
                 correlation_id: str = None, seq: int = None,
                 percent: int = None, model_name: str = None,
                 tokens_in: int = None, tokens_out: int = None,
                 duration_ms: int = None, error_message: str = None,
                 detail: Dict = None) -> bool:
        """Enfileira UM passo. Nivel baixo — prefira `agent_step`.

        `label` e' obrigatorio e vai para humano. Passo sem frase legivel e' log
        de aplicacao, e para isso existe `log_application`.
        """
        if not self.config.enabled:
            return False
        if not (label or '').strip():
            self.logger.debug("log_step ignorado: label vazio")
            return False
        if not self.config.has_logging_identity():
            self.logger.debug(
                "log_step pulado: product_id/environment_id nao configurados"
            )
            return False

        # Herda o run do JobRunner quando emitido de dentro de um handler.
        if not run_id:
            try:
                from .jobs import current_run_context  # noqa: PLC0415
                ctx = current_run_context()
                if ctx and getattr(ctx, 'run_id', None):
                    run_id = ctx.run_id
            except Exception:
                pass

        # Sem run, cai na correlacao do escopo (se houver).
        if not run_id and not correlation_id:
            correlation_id = getattr(_step_local, 'correlation_id', None)

        payload = {
            "product_id": str(self.config.product_id),
            "environment_id": str(self.environment_id),
            "seq": seq if seq is not None else self._next_step_seq(),
            "kind": (kind or 'step')[:20],
            "label": label,
            "status": (status or 'ok')[:16],
            "at": datetime.utcnow().isoformat(),
        }

        # UUIDs invalidos sao DESCARTADOS, nunca enviados: o contrato tipa os
        # campos como UUID e um valor torto derrubaria o lote inteiro do outro
        # lado — levando junto os passos validos que vierem no mesmo POST.
        for chave, valor in (("run_id", run_id), ("correlation_id", correlation_id),
                             ("agent_id", agent_id), ("area_agent_id", area_agent_id),
                             ("connection_id", connection_id)):
            if not valor:
                continue
            try:
                payload[chave] = str(UUID(str(valor)))
            except (ValueError, AttributeError, TypeError):
                self.logger.warning(
                    "log_step: %s='%s' nao e' UUID valido — dimensao omitida (%s)",
                    chave, valor, label
                )

        for chave, valor in (("percent", percent), ("model_name", model_name),
                             ("tokens_in", tokens_in), ("tokens_out", tokens_out),
                             ("duration_ms", duration_ms),
                             ("error_message", error_message)):
            if valor is not None:
                payload[chave] = valor
        if detail:
            payload["detail"] = detail

        return self._enqueue_safely("execution_step", payload)

    @contextmanager
    def agent_step(self, agent_slug: str = None, label: str = None,
                   kind: str = 'step', connection_id: str = None,
                   area_agent_slug: str = None, detail: Dict = None,
                   stream: bool = True):
        """Marca uma etapa do trabalho de um agente.

            with admin.agent_step('harvest-mapeador-sql',
                                  label='mapeando o array de entrada') as passo:
                passo.progress(40, 'validando colunas contra o schema')
                resultado = mapear()
                passo.done(tokens_in=pt, tokens_out=ct)

        O que ele resolve sozinho:

        - **`agent_id` e o modelo EFETIVO** do slug, pelo mesmo
          `_resolve_agent_info` (cacheado, com cache de negativa) que o token
          tracking usa. Por isso o passo grava o modelo que DE FATO rodou —
          `product_agents.model_id` vence `agents.model_id` e muda sem deploy.
          O Harvest ja' pagou por essa licao gravando `OPENAI_MODEL` no artefato.
        - **`run_id`** herdado do run context quando dentro de um job;
          `correlation_id` do `execution_scope` quando fora.
        - **`duration_ms`** e o `status` final: `failed` quando a excecao sobe
          (e ela CONTINUA subindo — telemetria nao engole erro do produto).

        `stream=True` (padrao) grava uma linha `running` na entrada, para um
        passo travado ficar visivel enquanto ainda esta' travado. `stream=False`
        grava so' a linha final — metade das linhas, nenhuma visibilidade ao
        vivo. Escolha por volume, sabendo o que perde.
        """
        texto = (label or agent_slug or 'etapa').strip()
        seq = self._next_step_seq()
        info = self._resolve_agent_info(agent_slug) if agent_slug else {}
        agent_id = info.get("agent_id")
        model_name = info.get("model_name")
        area_agent_id = self.resolve_agent_id(area_agent_slug) if area_agent_slug else None

        handle = _StepHandle(self, texto, kind, seq, agent_id, area_agent_id,
                             connection_id, model_name, detail)

        if stream:
            self.log_step(texto, kind=kind, status='running', seq=seq,
                          agent_id=agent_id, area_agent_id=area_agent_id,
                          connection_id=connection_id, model_name=model_name,
                          percent=0, detail=detail)

        inicio = time.time()
        try:
            yield handle
        except Exception as e:
            handle._encerrar(inicio, 'failed', str(e))
            raise
        else:
            handle._encerrar(inicio, handle._status, handle._erro)

    # ==================== PROMPTS ====================

    def get_prompt(self, slug: str, organization_id: str = None) -> Optional[Dict]:
        """
        Busca prompt por slug no AdminCenter.
        Permite que projetos usem prompts centralizados ao inves de hardcoded.

        Args:
            slug: Slug unico do prompt (ex: 'talk-sql-agent')
            organization_id: ID da organização (usa o configurado se nao informado)

        Returns:
            Dict com dados do prompt (content, temperature, max_tokens, etc.) ou None
        """
        if not self.config.enabled:
            return None

        org_id = organization_id or self.config.organization_id
        endpoint = AdminCenterEndpoints.PROMPT_BY_SLUG.format(slug)
        params = {"organization_id": org_id}

        response = self._make_request("GET", endpoint, params=params)

        if response and "data" in response:
            self.logger.debug(f"Prompt '{slug}' carregado do AdminCenter")
            return response["data"]

        self.logger.warning(f"Prompt '{slug}' nao encontrado no AdminCenter")
        return None

    def get_prompt_by_id(self, prompt_id: str) -> Optional[Dict]:
        """
        Busca prompt por ID no AdminCenter.

        Args:
            prompt_id: UUID do prompt

        Returns:
            Dict com dados do prompt ou None
        """
        if not self.config.enabled:
            return None

        params = {"prompt_id": prompt_id}
        response = self._make_request("GET", AdminCenterEndpoints.PROMPT_BY_ID, params=params)

        if response and "data" in response:
            return response["data"]

        return None

    def get_prompts(self, organization_id: str = None, agent_id: str = None,
                    tags: List[str] = None, is_active: bool = True) -> List[Dict]:
        """
        Lista prompts no AdminCenter.

        Args:
            organization_id: ID da organização (usa o configurado se nao informado)
            agent_id: ID do agente para filtrar prompts vinculados (opcional)
            tags: Filtrar por tags (ex: ['analise', 'geracao'])
            is_active: Filtrar por status ativo (default True)

        Returns:
            Lista de prompts ou lista vazia
        """
        if not self.config.enabled:
            return []

        endpoint = AdminCenterEndpoints.PROMPTS_LIST
        params = {"is_active": str(is_active).lower()}

        org_id = organization_id or self.config.organization_id
        if org_id:
            params["organization_id"] = org_id

        if agent_id:
            params["agent_id"] = agent_id

        if tags:
            params["tags"] = ",".join(tags)

        response = self._make_request("GET", endpoint, params=params)

        if response and "data" in response:
            data = response["data"]
            return data if isinstance(data, list) else []

        return []

    def get_effective_prompt(self, agent_slug: str, product_id: str = None) -> Optional[Dict]:
        """
        Resolve o prompt efetivo de um agente para um produto.

        Retorna os prompts genéricos do agente (todos os vinculados, já concatenados
        em generic_content) e, se existir, a customização do produto (custom_content).

        Args:
            agent_slug: Slug do agente (ex: 'sql-analyst', 'summarizer')
            product_id: UUID do produto. Se omitido, usa ADMIN_CENTER_PRODUCT_ID do .env.

        Returns:
            Dict com os campos:
              - generic_content: str  (prompts base do agente, prontos para system message)
              - generic_prompts: list (lista dos prompts individuais com id, name, content)
              - generic_temperature: float
              - generic_max_tokens: int
              - custom_content: str | None (instrução adicional do produto, se houver)
              - is_customized: bool
              - is_prompt_selection_active: bool
              - selected_prompt_ids: list[str]
              - model_id: str | None       (modelo de IA EFETIVO configurado: override
                                            do produto → padrão do agente)
              - model_name: str | None     (nome do modelo, ex.: 'gpt-4o')
              - model_display_name: str | None
              - is_model_overridden: bool  (True se o produto sobrescreveu o modelo do agente)
            None se o agente não estiver vinculado ao produto.

        Use `model_name`/`model_id` para escolher o LLM na inferência e passe o
        mesmo em `track_token_usage` (ou apenas informe `agent_slug` lá, que a lib
        resolve o modelo do agente automaticamente).

        Exemplo de uso:
            ep = admin.get_effective_prompt('sql-analyst')
            system_parts = [ep['generic_content']]
            if ep.get('custom_content'):
                system_parts.append(ep['custom_content'])
            system_message = '\\n\\n---\\n\\n'.join(system_parts)
            modelo = ep.get('model_name')  # modelo configurado no agente
            # ... chama o LLM com `modelo` ...
            admin.track_token_usage(agent_slug='sql-analyst',
                                    prompt_tokens=pt, completion_tokens=ct)
        """
        if not self.config.enabled:
            return None

        pid = product_id or self.config.product_id
        endpoint = AdminCenterEndpoints.EFFECTIVE_PROMPT.format(pid, agent_slug)
        response = self._make_request("GET", endpoint)

        if response and "data" in response:
            self.logger.debug(f"Effective prompt para agente '{agent_slug}' carregado")
            return response["data"]

        self.logger.warning(f"Agente '{agent_slug}' não vinculado ao produto {pid}")
        return None

    # ------------------------------------------------------------------
    # RBAC por perfil (role_agents / role_connections, migration 0037)
    # ------------------------------------------------------------------
    # Estes quatro metodos autenticam com o **JWT do USUARIO**, nao com a
    # api-key do produto: quem o AdminCenter precisa resolver e a sessao de
    # quem esta perguntando. Passe adiante o mesmo bearer que chegou na
    # requisicao do produto.

    def list_allowed_agents(self, user_bearer_token: str,
                            product_id: str = None) -> List[Dict]:
        """Agentes que o USUARIO pode usar, conforme os agentes vinculados aos
        perfis dele (`role_agents`).

        Args:
            user_bearer_token: JWT do usuario, sem o prefixo "Bearer ".
            product_id: restringe aos agentes do produto (via `product_agents`).
                Omitido, usa o `product_id` da config.

        Returns:
            Lista de dicts {id, slug, name, description, type, is_restricted}.
            Lista VAZIA quando desabilitado, sem token ou em erro — nunca
            levanta.

        Regra aplicada do lado do AdminCenter: agente sem NENHUM vinculo de
        perfil e liberado para toda a organizacao (`is_restricted=False`); com
        vinculo, so aparece para quem tem o perfil (`is_restricted=True`).
        Quem tem `system:full_access` ve todos.
        """
        if not self.config.enabled:
            return []
        if not user_bearer_token:
            self.logger.warning("list_allowed_agents chamado sem user_bearer_token")
            return []

        pid = product_id or self.config.product_id
        cache_key = f"{user_bearer_token}:{pid or ''}"
        now = time.time()

        with self._allowed_agents_lock:
            cached = self._allowed_agents_cache.get(cache_key)
            if cached and (now - cached[0]) < self._allowed_rbac_ttl:
                return cached[1]

        url = f"{self.config.api_url}{AdminCenterEndpoints.AGENTS_ALLOWED}"
        headers = {
            "Authorization": f"Bearer {user_bearer_token}",
            "Content-Type": "application/json",
        }
        params = {"product_id": pid} if pid else None

        try:
            resp = requests.get(url, headers=headers, params=params,
                                timeout=self.config.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data") or []
                agents = data if isinstance(data, list) else []
                with self._allowed_agents_lock:
                    self._allowed_agents_cache[cache_key] = (now, agents)
                return agents
            self.logger.warning(
                f"list_allowed_agents HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return []
        except requests.RequestException as e:
            self.logger.warning(f"list_allowed_agents erro de conexao: {e}")
            return []

    def is_agent_allowed(self, user_bearer_token: str, agent_slug: str,
                         product_id: str = None) -> bool:
        """True se `agent_slug` esta entre os agentes permitidos ao usuario.

        Conveniencia sobre `list_allowed_agents` — use para validar do lado do
        servidor a escolha de agente antes de resolver prompt/modelo.

        FAIL-SAFE: erro ou lista vazia devolve False (NEGA). Escolha
        deliberada — em RBAC, indisponibilidade nao pode virar permissao. Mas
        veja o efeito colateral: com o endpoint fora do ar, o produto nega
        tudo em silencio. Se o log mostrar negativas em massa, suspeite da
        conectividade antes de suspeitar da configuracao de perfis.
        """
        if not agent_slug:
            return False
        allowed = self.list_allowed_agents(user_bearer_token, product_id)
        return any(a.get("slug") == agent_slug for a in allowed)

    def list_allowed_connection_ids(self, user_bearer_token: str) -> Dict:
        """Conexoes do cofre que o PERFIL do usuario libera (`role_connections`).

        Returns:
            {"restricted": bool, "connection_ids": [str]}. `restricted=False`
            significa SEM restricao (full_access, ou perfil sem nenhum vinculo)
            — qualquer conexao da organizacao vale.

        FAIL-OPEN: erro ou ausencia de token devolve `restricted=False`.
        Oposto do `is_agent_allowed` de proposito: aqui a barreira dura e a
        credencial da propria conexao, resolvida pelo cofre. Esta lista e
        curadoria, nao contencao — negar por indisponibilidade tiraria o
        produto do ar sem ganho de seguranca real.
        """
        default = {"restricted": False, "connection_ids": []}
        if not self.config.enabled or not user_bearer_token:
            return default

        now = time.time()
        with self._allowed_conns_lock:
            cached = self._allowed_conns_cache.get(user_bearer_token)
            if cached and (now - cached[0]) < self._allowed_rbac_ttl:
                return cached[1]

        url = f"{self.config.api_url}{AdminCenterEndpoints.CONNECTIONS_ALLOWED}"
        headers = {
            "Authorization": f"Bearer {user_bearer_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.get(url, headers=headers, timeout=self.config.timeout)
            if resp.status_code == 200:
                data = resp.json().get("data") or {}
                out = {
                    "restricted": bool(data.get("restricted")),
                    "connection_ids": [str(c) for c in (data.get("connection_ids") or [])],
                }
                with self._allowed_conns_lock:
                    self._allowed_conns_cache[user_bearer_token] = (now, out)
                return out
            self.logger.warning(
                f"list_allowed_connection_ids HTTP {resp.status_code}: {resp.text[:200]}"
            )
            return default
        except requests.RequestException as e:
            self.logger.warning(f"list_allowed_connection_ids erro de conexao: {e}")
            return default

    def is_connection_allowed(self, user_bearer_token: str, connection_id: str) -> bool:
        """True se o usuario pode usar a conexao (RBAC de dados por perfil).

        `restricted=False` (full_access, perfil sem vinculo, ou erro) devolve
        True — fail-open, so o filtro de organizacao do produto vale.
        `restricted=True` exige a conexao na lista liberada.
        """
        if not connection_id:
            return True
        info = self.list_allowed_connection_ids(user_bearer_token)
        if not info.get("restricted"):
            return True
        return str(connection_id) in (info.get("connection_ids") or [])

    def log_prompt_usage(self, prompt_id: str, product_id: str = None,
                         environment_id: str = None, request_id: str = None,
                         variables_used: Dict = None, final_prompt: str = None,
                         tokens_used: int = None, model_used: str = None) -> bool:
        """
        Registra uso de um prompt (para analytics por prompt).

        Args:
            prompt_id: UUID do prompt utilizado
            variables_used: Variaveis substituidas no prompt (ex: {'empresa': 'CASAN'})
            final_prompt: Prompt final apos substituicao de variaveis
            tokens_used: Total de tokens consumidos
            model_used: Nome do modelo LLM utilizado
        """
        if not self.config.enabled:
            return False

        payload = {
            "product_id": product_id or self.config.product_id,
            "environment_id": environment_id or self.environment_id,
            "request_id": request_id or str(uuid.uuid4()),
            "variables_used": variables_used or {},
            "final_prompt": final_prompt[:2000] if final_prompt else None,
            "tokens_used": tokens_used,
            "model_used": model_used
        }

        # Remover None
        payload = {k: v for k, v in payload.items() if v is not None}

        # _prompt_id e usado pelo batch worker para montar o endpoint dinamico
        payload["_prompt_id"] = prompt_id
        return self._enqueue_safely("prompt_usage", payload)

    # ==================== BATCH PROCESSING OTIMIZADO ====================
    
    def _start_batch_worker(self):
        """Inicia worker thread para processar batch"""
        self._worker_thread = Thread(target=self._batch_worker, daemon=True)
        self._worker_thread.start()
        self.logger.info("Batch worker assíncrono iniciado para máxima performance")
    
    def _batch_worker(self):
        """Worker que processa a fila de requisições em batch - OTIMIZADO"""
        while not self._shutdown:
            try:
                batch = []
                deadline = time.time() + self.config.batch_interval
                
                while time.time() < deadline and len(batch) < self.config.batch_size:
                    try:
                        timeout = max(0.1, deadline - time.time())
                        item = self._queue.get(timeout=timeout)
                        batch.append(item)
                    except Empty:
                        break
                
                if batch:
                    self._process_batch(batch)
                    
            except Exception as e:
                self.logger.error(f"Erro no batch worker: {e}")
                time.sleep(0.5)  # Pausa menor para recuperação rápida
    
    @staticmethod
    def _envelope_aceito(resposta) -> bool:
        """True se o AdminCenter de fato GRAVOU o que foi enviado.

        A API responde num envelope `{success, data, message, status_code}` e
        ha' rotas de escrita que recusam o registro (produto de outro tenant,
        lote acima do teto) devolvendo `success: false` DENTRO de um 201 —
        `_make_request` so' olha o codigo HTTP e devolve o corpo, entao a
        recusa chegava aqui indistinguivel de um insert. Sem esta checagem o
        batch worker contabiliza "1/1 enviados" para uma linha que nunca
        existiu, e o produto fica sem nenhum sinal de que perdeu telemetria.
        """
        if not isinstance(resposta, dict):
            return bool(resposta)
        return resposta.get("success") is not False

    def _process_batch(self, batch: List[tuple]):
        """Processa um batch de requisições enviando individualmente"""
        success_count = 0
        
        endpoint_map = {
            "token_usage": AdminCenterEndpoints.TOKEN_USAGE,
            "log_execution": AdminCenterEndpoints.LOG_EXECUTION,
            "log_application": AdminCenterEndpoints.LOG_APPLICATION,
            "log_process": AdminCenterEndpoints.LOG_PROCESS,
            "prompt_usage": None  # endpoint dinamico, tratado abaixo
        }
        
        # `execution_step` e' o unico tipo cujo endpoint recebe LISTA. Agrupar
        # aqui e' o que torna o passo barato: um pipeline de 16 etapas vira 1
        # POST, nao 32. Sem isso, a granularidade fina que a tabela existe para
        # permitir sairia cara justamente onde ela mais serve.
        passos = [p for tipo, p in batch if tipo == "execution_step"]
        if passos:
            try:
                resp = self._make_request("POST", AdminCenterEndpoints.LOG_STEP, passos)
                if self._envelope_aceito(resp):
                    success_count += len(passos)
                else:
                    self.logger.warning(
                        "AdminCenter RECUSOU %d passos de execucao: %s",
                        len(passos),
                        (resp or {}).get("message") if isinstance(resp, dict) else "sem resposta",
                    )
            except Exception as e:
                self.logger.debug(f"Erro ao enviar lote de passos: {e}")

        for endpoint_type, payload in batch:
            try:
                if endpoint_type == "execution_step":
                    continue  # ja' foi, em lote

                endpoint = endpoint_map.get(endpoint_type)

                # prompt_usage tem endpoint dinamico com prompt_id
                if endpoint_type == "prompt_usage":
                    prompt_id = payload.pop("_prompt_id", None)
                    if prompt_id:
                        endpoint = AdminCenterEndpoints.PROMPT_LOG_USAGE.format(prompt_id)
                    else:
                        self.logger.debug("prompt_usage sem prompt_id, ignorando")
                        continue

                if endpoint:
                    response = self._make_request("POST", endpoint, payload)
                    if self._envelope_aceito(response):
                        success_count += 1
                    else:
                        # WARNING, nao debug: aqui a linha e' PERDIDA. Com debug,
                        # a perda so' aparecia para quem ja' desconfiava dela.
                        self.logger.warning(
                            "AdminCenter RECUSOU %s: %s",
                            endpoint_type,
                            response.get("message") if isinstance(response, dict)
                            else "sem resposta (rede/HTTP)",
                        )

            except Exception as e:
                self.logger.debug(f"Erro ao processar item do batch {endpoint_type}: {e}")
        
        if success_count > 0:
            self.logger.debug(f"Batch processado: {success_count}/{len(batch)} items enviados")
    
    # ==================== LIFECYCLE OTIMIZADO ====================
    
    def flush(self):
        """Força envio de todos os items pendentes na fila - NÃO BLOQUEIA"""
        if not self.config.enabled:
            return
        
        items = []
        # Limite para não travar o shutdown
        max_items = 100
        
        while not self._queue.empty() and len(items) < max_items:
            try:
                items.append(self._queue.get_nowait())
            except Empty:
                break
        
        if items:
            self._process_batch(items)
            self.logger.info(f"Flush rápido executado: {len(items)} items enviados")
    
    def shutdown(self):
        """Finaliza o serviço - OTIMIZADO PARA NÃO TRAVAR"""
        if not self.config.enabled:
            return
        
        self.logger.info("Finalizando Admin Center Service...")
        
        self._shutdown = True
        
        # Flush rápido sem travar
        self.flush()
        
        # Aguardar worker por tempo limitado
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2)  # Máximo 2 segundos

        # Fecha tuneis SSH abertos pelo ConnectionResolver, se houver
        if self._connection_resolver is not None:
            try:
                self._connection_resolver.shutdown()
            except Exception as e:
                self.logger.warning(f"Erro ao finalizar ConnectionResolver: {e}")

        if self._session:
            self._session.close()

        self.logger.info("Admin Center Service finalizado rapidamente")
    
    def __del__(self):
        """Destructor para garantir limpeza"""
        try:
            self.shutdown()
        except:
            pass


# ==================== SINGLETON INSTANCE ====================

_admin_center_instance = None
_instance_lock = Lock()

def get_admin_center_service(config: AdminCenterConfig = None) -> AdminCenterService:
    """
    Obtém instância singleton do AdminCenterService
    """
    global _admin_center_instance
    
    with _instance_lock:
        if _admin_center_instance is None:
            _admin_center_instance = AdminCenterService(config)
    
    return _admin_center_instance


def reset_admin_center_service():
    """Reset da instância singleton (útil para testes)"""
    global _admin_center_instance
    
    with _instance_lock:
        if _admin_center_instance:
            _admin_center_instance.shutdown()
        _admin_center_instance = None


# ==================== CONTEXT MANAGER ====================

class AdminCenterContext:
    """Context manager para uso seguro do Admin Center Service"""
    
    def __init__(self, config: AdminCenterConfig = None):
        self.config = config
        self.service = None
    
    def __enter__(self) -> AdminCenterService:
        self.service = AdminCenterService(self.config)
        return self.service
    
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.service:
            self.service.shutdown()


# ==================== DECORATORS ====================

def track_execution(process_name: str = None):
    """
    Decorator para tracking automático de execução
    """
    def decorator(func):
        def wrapper(*args, **kwargs):
            admin = get_admin_center_service()
            name = process_name or func.__name__
            
            admin.log_process(name, "started")
            start_time = time.time()
            
            try:
                result = func(*args, **kwargs)
                duration_ms = int((time.time() - start_time) * 1000)
                
                output_data = {}
                if hasattr(result, '__dict__'):
                    output_data = {"type": type(result).__name__}
                elif isinstance(result, (dict, list, str, int, float, bool)):
                    output_data = {"result": str(result)[:500] if isinstance(result, str) else result}
                
                admin.log_process(
                    process_name=name, 
                    status="completed", 
                    duration_ms=duration_ms,
                    output_data=output_data
                )
                return result
                
            except Exception as e:
                duration_ms = int((time.time() - start_time) * 1000)
                
                admin.log_process(
                    process_name=name, 
                    status="failed", 
                    duration_ms=duration_ms,
                    error_message=str(e),
                    metadata={"error_type": type(e).__name__}
                )
                
                admin.log_application(
                    level="error", 
                    message=f"Erro em {name}: {str(e)}",
                    context={"function": name, "duration_ms": duration_ms}
                )
                raise
        
        return wrapper
    return decorator