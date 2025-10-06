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
from threading import Thread, Lock
from queue import Queue, Empty
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
    
    @classmethod
    def from_env(cls, prefix: str = "ADMIN_CENTER"):
        """Cria configuração a partir de variáveis de ambiente"""
            
        environment = os.getenv("ENVIRONMENT", "production")
        api_url = os.getenv(f"{prefix}_URL", "")
        
        if environment == 'development':
            api_url_local = os.getenv(f"{prefix}_URL_LOCAL", "")
            if api_url_local:
                api_url = api_url_local

        return cls(
            api_url=api_url,
            api_key=os.getenv(f"{prefix}_API_KEY", ""),
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
            queue_max_size=int(os.getenv(f"{prefix}_QUEUE_MAX_SIZE", "1000"))
        )
    
    def is_valid(self) -> bool:
        """Verifica se a configuração é válida"""
        if not self.enabled:
            return True
        return all([self.api_url, self.api_key, self.product_id, self.environment_id])


class AdminCenterEndpoints:
    """Endpoints do Admin Center API"""
    TOKEN_USAGE = "/token-usage/"
    LOG_EXECUTION = "/logs/execution"
    LOG_APPLICATION = "/logs/application"
    LOG_PROCESS = "/logs/process"
    SECRET_DECRYPT = "/secret/decrypt"
    ENVIRONMENT_VARIABLES = "/environment/{}/variables"
    AI_MODEL_BY_NAME = "/ai-model/consulta_nome"
    AUTH_TOKEN = "/auth/gerar-token/api-key"


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
        
        # Sistema de batch assíncrono OTIMIZADO
        self._queue = Queue(maxsize=self.config.queue_max_size)
        self._batch_lock = Lock()
        self._worker_thread = None
        self._shutdown = False

        # Resolver environment_id baseado no ambiente
        self.environment_id = self._resolve_environment_id()
        self.environment_name = self.config.environment_name
        
        self._model_cache = {}
        self._cache_lock = Lock()
        
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
            
            # SEMPRE iniciar worker assíncrono para máxima performance
            self._start_batch_worker()
            
            self.logger.info(f"Admin Center Service inicializado (ASYNC MODE) - Produto: {self.config.product_id}")
            
        except Exception as e:
            self.logger.error(f"Erro ao inicializar Admin Center Service: {e}")
            self.config.enabled = False
    
    def _get_access_token(self):
        """Obtém token de acesso usando API key"""
        url = f"{self.config.api_url}{AdminCenterEndpoints.AUTH_TOKEN}"
        headers = {'api-key': self.config.api_key}
        
        try:
            response = requests.post(url, headers=headers, timeout=self.config.timeout)
            response.raise_for_status()
            
            data = response.json()
            self.access_token = data['data']['access_token']
            self.logger.info("Token de acesso obtido com sucesso")
            
        except Exception as e:
            self.logger.error(f"Erro ao obter token: {e}")
            raise
    
    def _setup_session(self):
        """Configura sessão HTTP reutilizável"""
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json"
        })
    
    def _make_request(self, method: str, endpoint: str, data: Dict = None, 
                     params: Dict = None, retry_count: int = 0) -> Optional[Dict]:
        """Executa requisição HTTP com retry automático"""
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
                return response.json()
            elif response.status_code == 422:
                try:
                    error_detail = response.json()
                    self.logger.error(f"Erro de validação 422 em {endpoint}: {json.dumps(error_detail, indent=2)}")
                except:
                    self.logger.error(f"Erro de validação 422 em {endpoint}: {response.text}")
                return None
            elif response.status_code == 401:
                self.logger.warning("Token expirado, tentando renovar...")
                self._get_access_token()
                self._setup_session()
                if retry_count < 1:
                    return self._make_request(method, endpoint, data, params, retry_count + 1)
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
    
    def track_token_usage(self, model_name: str, prompt_tokens: int, 
                         completion_tokens: int, request_id: str = None,
                         user_id: str = None, endpoint_called: str = None, metadata: Dict = {}) -> bool:
        """
        Registra uso de tokens de IA - SEMPRE ASSÍNCRONO para máxima performance
        """
        if not self.config.enabled:
            return False
        
        model_id = self._get_model_id_by_name(model_name)
        if not model_id:
            self.logger.warning(f"Model ID não encontrado para '{model_name}'. Pulando registro.")
            return False
        
        if not request_id:
            request_id = str(uuid.uuid4())
        
        payload = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
            "model_id": model_id,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "alert_metadata": {
                "endpoint": endpoint_called or "/unknown",
                "model_name": model_name,
                **metadata
            }
        }

        if request_id:
            payload["request_id"] = request_id
        if user_id:
            payload["user_id"] = user_id
        
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
                self.logger.debug("Todo cache de modelos invalidado")

    def log_application(self, level: str, message: str, stack_trace: str = None,
                       context: Dict = None) -> bool:
        """
        Registra log de aplicação - SEMPRE ASSÍNCRONO
        """
        if not self.config.enabled:
            return False
        
        payload = {
            "product_id": self.config.product_id,
            "environment_id": self.environment_id,
            "log_level": level.upper(),
            "message": message,
            "stack_trace": stack_trace,
            "context": context or {},
            "timestamp": datetime.utcnow().isoformat()
        }
        
        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("log_application", payload)
    
    def log_execution(self, endpoint: str, method: str, status_code: int,
                     response_time_ms: int, error: str = None) -> bool:
        """
        Registra log de execução HTTP - SEMPRE ASSÍNCRONO
        """
        if not self.config.enabled:
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
               output_data: Dict = None) -> bool:
        """
        Registra log de processo de negócio - SEMPRE ASSÍNCRONO
        """
        if not self.config.enabled:
            return False
            
        try:
            product_uuid = UUID(self.config.product_id)
            environment_uuid = UUID(self.environment_id)
        except ValueError as e:
            self.logger.error(f"IDs inválidos: product_id={self.config.product_id}, environment_id={self.environment_id}")
            return False
        
        now = datetime.utcnow()
        
        payload = {
            "product_id": str(product_uuid),
            "environment_id": str(environment_uuid),
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
        
        # Remover campos None para evitar problemas de serialização
        payload = {k: v for k, v in payload.items() if v is not None}
        
        # SEMPRE ASSÍNCRONO - nunca bloqueia a aplicação
        return self._enqueue_safely("log_process", payload)
    
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
    
    def _process_batch(self, batch: List[tuple]):
        """Processa um batch de requisições enviando individualmente"""
        success_count = 0
        
        endpoint_map = {
            "token_usage": AdminCenterEndpoints.TOKEN_USAGE,
            "log_execution": AdminCenterEndpoints.LOG_EXECUTION,
            "log_application": AdminCenterEndpoints.LOG_APPLICATION,
            "log_process": AdminCenterEndpoints.LOG_PROCESS
        }
        
        for endpoint_type, payload in batch:
            try:
                endpoint = endpoint_map.get(endpoint_type)
                if endpoint:
                    response = self._make_request("POST", endpoint, payload)
                    if response:
                        success_count += 1
                    else:
                        self.logger.debug(f"Falha ao enviar {endpoint_type} - continuando processamento")
                        
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