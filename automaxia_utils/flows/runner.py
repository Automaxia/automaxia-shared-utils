"""FlowRunner — executa um fluxo de agentes (1.19.0).

Contrato no SDD do ecossistema, §5.11 (formato do fluxo) e §5.12 (produtos
derivados). O desenho vive no produto que hospeda (o Forge); este modulo e' o
interpretador, o mesmo para qualquer produto.

## Formato (`formato: 1`)

    {"formato": 1,
     "nos": [{"id": "gerar", "tipo": "agente", "rotulo": "Gerar SQL",
              "agente": "agent-sql", "instrucao": "...",
              "entradas": {"pergunta": "$entrada.pergunta",
                           "sql": "$nos.corretor.sql | $nos.gerar.sql"},
              "saida": {"formato": "json", "campos": ["sql"]}}, ...],
     "arestas": [{"de": "validar", "para": "ok", "ramo": "ok"},
                 {"de": "corretor", "para": "validar", "max_voltas": 3}]}

Tipos de no: `entrada` (implicito), `agente`, `ferramenta`, `condicao`, `saida`.
Referencia e' CAMINHO, nunca expressao: `$entrada.<campo>`, `$nos.<id>.<campo>`,
com alternativas `a | b` (vale a primeira que existir).

## Regras que nao dependem do desenho

- **IA so' do AdminCenter**: prompt e modelo de cada no agente vem de
  `get_effective_prompt(slug)` do produto em escopo. Faltou vinculo ou modelo,
  `IaNaoConfigurada` com a causa — nunca um texto ou modelo de reserva.
- **Teste nao age**: em `modo='teste'`, ferramenta de EFEITO nem e' chamada; o
  no devolve o que faria. Quem decide e' o motor, nao o fluxo nem a ferramenta.
- **Passo nao e' run**: cada no vira um `agent_step`; o run faturado
  (`log_process`) e' de quem chama o runner, um por execucao.
"""
from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger(__name__)

OPERADORES = ('igual', 'diferente', 'existe', 'vazio', 'contem', 'maior', 'menor')
TETO_NOS = 60


# ------------------------------------------------------------ erros

class FluxoInvalido(ValueError):
    """O desenho nao pode rodar (validacao estatica)."""


class IaNaoConfigurada(RuntimeError):
    """Falta cadastro no AdminCenter (agente nao vinculado, sem modelo)."""


class LimiteExcedido(RuntimeError):
    """Passou de um limite da execucao (nos, tokens, tempo, voltas)."""


class FalhaDoNo(RuntimeError):
    """Um no falhou; carrega o id para o rastro."""

    def __init__(self, no_id: str, causa: str):
        super().__init__(causa)
        self.no_id = no_id


# ------------------------------------------------------------ ferramentas

@dataclass
class ContextoFluxo:
    """O que a ferramenta recebe alem das entradas do no."""
    modo: str                       # 'real' | 'teste'
    entrada: Dict[str, Any]         # a entrada do fluxo
    product_id: Optional[str] = None
    extra: Dict[str, Any] = field(default_factory=dict)   # o que o produto quiser passar (conexao, usuario...)


@dataclass
class Ferramenta:
    nome: str
    funcao: Callable[[ContextoFluxo, Dict[str, Any], Dict[str, Any]], Dict[str, Any]]
    versao: int = 1
    descricao: str = ''
    entradas: List[str] = field(default_factory=list)
    saida: List[str] = field(default_factory=list)
    selada: bool = False            # guardas internas que nenhum desenho desliga
    efeito: Optional[str] = None    # 'email' | 'whatsapp' | 'webhook' | ... — age fora da plataforma

    def spec(self) -> Dict[str, Any]:
        """Declaracao para o manifest (`tools`, SDD §5.11.3)."""
        d = {'name': self.nome, 'version': self.versao, 'descricao': self.descricao,
             'entradas': list(self.entradas), 'saida': list(self.saida), 'selada': self.selada}
        if self.efeito:
            d['efeito'] = self.efeito
        return d


class RegistroDeFerramentas:
    """As ferramentas que um produto oferece aos fluxos.

        registro = RegistroDeFerramentas()

        @registro.ferramenta('executar_sql', entradas=['sql'], saida=['linhas'], selada=True)
        def executar_sql(ctx, entradas, config):
            ...
    """

    def __init__(self):
        self._itens: Dict[str, Ferramenta] = {}

    def ferramenta(self, nome: str, versao: int = 1, descricao: str = '', entradas=(), saida=(),
                   selada: bool = False, efeito: Optional[str] = None):
        def decorar(funcao):
            self.adicionar(Ferramenta(nome=nome, funcao=funcao, versao=versao, descricao=descricao,
                                      entradas=list(entradas), saida=list(saida), selada=selada, efeito=efeito))
            return funcao
        return decorar

    def adicionar(self, f: Ferramenta) -> None:
        if f.nome in self._itens:
            raise ValueError(f"Ferramenta '{f.nome}' registrada duas vezes")
        self._itens[f.nome] = f

    def get(self, nome: str) -> Optional[Ferramenta]:
        return self._itens.get(nome)

    def nomes(self) -> List[str]:
        return sorted(self._itens)

    def specs(self) -> List[Dict[str, Any]]:
        return [self._itens[n].spec() for n in self.nomes()]


# ------------------------------------------------------------ LLM (injetado)

@dataclass
class PedidoLLM:
    modelo: str
    sistema: str
    usuario: str
    json: bool = False
    temperatura: Optional[float] = None     # ausente = nao enviar
    max_tokens: Optional[int] = None


@dataclass
class RespostaLLM:
    texto: str
    tokens_entrada: int = 0
    tokens_saida: int = 0


# ------------------------------------------------------------ definicao

def _alternativas(valor) -> List[str]:
    if valor is None:
        return []
    if isinstance(valor, (list, tuple)):
        return [str(v).strip() for v in valor if str(v).strip()]
    return [p.strip() for p in str(valor).split('|') if p.strip()]


def _portas(no: Dict[str, Any]) -> List[str]:
    if no.get('tipo') == 'agente' and (no.get('saida') or {}).get('formato') == 'escolha':
        return list(no['saida'].get('opcoes') or [])
    if no.get('tipo') == 'condicao':
        return [r['ramo'] for r in no.get('regras') or []] + ['senao']
    if no.get('tipo') == 'saida':
        return []
    return ['out']


def _ramificado(no) -> bool:
    return (no.get('tipo') == 'agente' and (no.get('saida') or {}).get('formato') == 'escolha') \
        or no.get('tipo') == 'condicao'


def _campos_de_saida(no, ferramentas: Optional[RegistroDeFerramentas], entradas_do_fluxo: List[str]) -> List[str]:
    tipo = no.get('tipo')
    if tipo == 'entrada':
        return list(entradas_do_fluxo)
    if tipo == 'ferramenta':
        f = ferramentas.get(no.get('ferramenta')) if ferramentas else None
        return list(f.saida) if f else []
    if tipo == 'agente':
        s = no.get('saida') or {'formato': 'texto'}
        if s.get('formato') == 'json':
            return list(s.get('campos') or [])
        if s.get('formato') == 'escolha':
            return ['escolha', 'motivo']
        return ['texto']
    return []


class _Grafo:
    def __init__(self, definicao: Dict[str, Any]):
        if not isinstance(definicao, dict) or definicao.get('formato') != 1:
            raise FluxoInvalido("Fluxo sem 'formato': 1")
        self.nos: Dict[str, Dict[str, Any]] = {}
        for no in definicao.get('nos') or []:
            if not no.get('id') or no['id'] in self.nos:
                raise FluxoInvalido(f"No sem id ou id repetido: {no.get('id')!r}")
            self.nos[no['id']] = no
        if 'entrada' not in self.nos:
            self.nos['entrada'] = {'id': 'entrada', 'tipo': 'entrada', 'rotulo': 'Entrada'}
        self.arestas: List[Dict[str, Any]] = []
        for i, a in enumerate(definicao.get('arestas') or []):
            e = {'id': f"{a.get('de')}>{a.get('para')}:{a.get('ramo') or 'out'}#{i}",
                 'de': a.get('de'), 'para': a.get('para'),
                 'porta': a.get('ramo') or 'out', 'max_voltas': a.get('max_voltas')}
            self.arestas.append(e)

    def saem(self, no_id):
        return [e for e in self.arestas if e['de'] == no_id]

    def chegam(self, no_id, com_voltas=False):
        return [e for e in self.arestas if e['para'] == no_id and (com_voltas or not e['max_voltas'])]

    def ancestrais(self, no_id) -> set:
        vistos, fila = set(), [no_id]
        while fila:
            u = fila.pop()
            for e in self.arestas:
                if e['para'] == u and e['de'] not in vistos:
                    vistos.add(e['de'])
                    fila.append(e['de'])
        return vistos  # o proprio no so' entra se estiver num laco

    def alcancaveis(self) -> set:
        vistos, fila = {'entrada'}, ['entrada']
        while fila:
            u = fila.pop()
            for e in self.saem(u):
                if e['para'] not in vistos:
                    vistos.add(e['para'])
                    fila.append(e['para'])
        return vistos


def validar_fluxo(definicao: Dict[str, Any], ferramentas: Optional[RegistroDeFerramentas] = None,
                  entradas: Optional[List[str]] = None, saidas: Optional[List[str]] = None) -> List[Dict[str, Any]]:
    """Problemas do desenho, sem executar nada: `[{nivel, no, msg}]`.

    `nivel='erro'` impede publicar; `'aviso'` nao. Nao confere agente contra o
    AdminCenter (o runner confere ao rodar, com a causa). `entradas` sao os
    campos que o ponto de entrada entrega; `saidas`, os que ele aceita de volta
    — o PRIMEIRO e' obrigatorio (ex.: `resposta`).
    """
    entradas = list(entradas or [])
    probs: List[Dict[str, Any]] = []

    def erro(msg, no=None):
        probs.append({'nivel': 'erro', 'no': no, 'msg': msg})

    try:
        g = _Grafo(definicao)
    except FluxoInvalido as exc:
        return [{'nivel': 'erro', 'no': None, 'msg': str(exc)}]

    if len(g.nos) > TETO_NOS:
        erro(f'O fluxo passou de {TETO_NOS} nos.')
    for e in g.arestas:
        if e['de'] not in g.nos or e['para'] not in g.nos:
            erro(f"Ligacao para no inexistente: {e['de']} -> {e['para']}")
        if e['max_voltas'] is not None and not (1 <= int(e['max_voltas']) <= 5):
            erro('O limite de voltas vai de 1 a 5.', e['de'])

    alc = g.alcancaveis()

    def checar_ref(ref, no_id):
        if ref.startswith('$entrada.'):
            campo = ref[len('$entrada.'):]
            return None if (not entradas or campo in entradas) else f'a entrada nao tem o campo "{campo}"'
        partes = ref.split('.')
        if len(partes) != 3 or partes[0] != '$nos':
            return f'"{ref}" nao e um caminho valido'
        origem = g.nos.get(partes[1])
        if origem is None:
            return f'o no "{partes[1]}" nao existe'
        if partes[1] not in g.ancestrais(no_id):
            return f'"{origem.get("rotulo") or partes[1]}" nao roda antes deste no'
        campos = _campos_de_saida(origem, ferramentas, entradas)
        if campos and partes[2] not in campos:
            return f'"{origem.get("rotulo") or partes[1]}" nao devolve o campo "{partes[2]}"'
        return None

    for no_id, no in g.nos.items():
        tipo = no.get('tipo')
        if tipo not in ('entrada', 'agente', 'ferramenta', 'condicao', 'saida'):
            erro(f'Tipo de no desconhecido: {tipo}', no_id)
            continue
        if tipo != 'entrada' and no_id not in alc:
            erro('No solto: nada leva ate ele.', no_id)
        if tipo == 'agente':
            if not no.get('agente'):
                erro('Escolha o agente.', no_id)
            s = no.get('saida') or {}
            if s.get('formato') == 'escolha' and len(s.get('opcoes') or []) < 2:
                erro('Uma escolha precisa de pelo menos duas opcoes.', no_id)
            if s.get('formato') == 'json' and not s.get('campos'):
                erro('Declare os campos da saida JSON.', no_id)
        if tipo == 'ferramenta':
            f = ferramentas.get(no.get('ferramenta')) if ferramentas else None
            if ferramentas and f is None:
                erro(f"O produto nao declara a ferramenta \"{no.get('ferramenta')}\".", no_id)
            if f:
                for c in f.entradas:
                    if not _alternativas((no.get('entradas') or {}).get(c)):
                        erro(f'Entrada "{c}" sem origem.', no_id)
                if f.efeito and not (no.get('config') or {}).get('destino'):
                    erro('Escolha o destino: envio so vai para destino cadastrado.', no_id)
        mapas = [no.get('entradas') or {}] + ([no.get('campos') or {}] if tipo == 'saida' else [])
        for mapa in mapas:
            for nome, valor in mapa.items():
                for ref in _alternativas(valor):
                    r = checar_ref(ref, no_id)
                    if r:
                        erro(f'"{nome}": {r}.', no_id)
        if tipo == 'condicao':
            for r in no.get('regras') or []:
                if r.get('op') not in OPERADORES:
                    erro(f"Operador desconhecido na regra \"{r.get('ramo')}\".", no_id)
                if not r.get('caminho'):
                    erro(f"Regra \"{r.get('ramo')}\" sem campo.", no_id)
                elif checar_ref(r['caminho'], no_id):
                    erro(f"Regra \"{r.get('ramo')}\": {checar_ref(r['caminho'], no_id)}.", no_id)
        if tipo == 'saida':
            preenchidos = [k for k, v in (no.get('campos') or {}).items() if _alternativas(v)]
            if not preenchidos:
                erro('A saida nao preenche nada.', no_id)
            for obrigatorio in (saidas or [])[:1]:
                if obrigatorio not in preenchidos:
                    erro(f'A saida precisa preencher "{obrigatorio}".', no_id)
        else:
            if _ramificado(no):
                for p in _portas(no):
                    if not any(e['porta'] == p for e in g.saem(no_id)):
                        erro(f'O ramo "{p}" nao leva a lugar nenhum.', no_id)
            elif not g.saem(no_id):
                erro('Termina aqui sem chegar a uma saida.', no_id)

    # laco sem limite: ciclo que sobra sem as arestas de volta
    grau = {n: 0 for n in g.nos}
    sem_volta = [e for e in g.arestas if not e['max_voltas'] and e['para'] in grau and e['de'] in grau]
    for e in sem_volta:
        grau[e['para']] += 1
    fila = [n for n, k in grau.items() if not k]
    vistos = 0
    while fila:
        u = fila.pop()
        vistos += 1
        for e in sem_volta:
            if e['de'] == u:
                grau[e['para']] -= 1
                if not grau[e['para']]:
                    fila.append(e['para'])
    if vistos < len(g.nos):
        erro('Ha um laco sem limite de voltas.')
    if not any(n.get('tipo') == 'saida' and i in alc for i, n in g.nos.items()):
        erro('Nenhuma saida e alcancavel.')
    return probs


# ------------------------------------------------------------ execucao

@dataclass
class ResultadoFluxo:
    saida: Optional[Dict[str, Any]]
    falha: Optional[str] = None
    no_falha: Optional[str] = None
    traco: List[Dict[str, Any]] = field(default_factory=list)
    tokens_entrada: int = 0
    tokens_saida: int = 0
    duracao_ms: int = 0
    correlation_id: Optional[str] = None

    @property
    def passos(self) -> int:
        return sum(1 for t in self.traco if t.get('tipo') in ('agente', 'ferramenta'))

    @property
    def ok(self) -> bool:
        return self.falha is None and self.saida is not None


class FlowRunner:
    """Interpreta um fluxo `formato: 1`.

        runner = FlowRunner(admin, registro, chamar_llm)
        with admin.product_scope(derivado_id):         # produto derivado
            r = runner.run(definicao, {'pergunta': '...'}, modo='real')

    `chamar_llm(PedidoLLM) -> RespostaLLM` e' do produto: a lib nao escolhe
    provedor. O runner chama `get_effective_prompt` para cada agente e rastreia
    tokens com `track_token_usage`, ambos no produto em escopo.
    """

    def __init__(self, admin, ferramentas: RegistroDeFerramentas,
                 chamar_llm: Callable[[PedidoLLM], RespostaLLM],
                 max_nos: int = TETO_NOS, max_tokens: Optional[int] = None,
                 max_segundos: Optional[float] = None, max_chars_entrada: int = 12000,
                 paralelo: int = 4):
        self.admin = admin
        self.ferramentas = ferramentas
        self.chamar_llm = chamar_llm
        self.max_nos = min(max_nos, TETO_NOS)
        self.max_tokens = max_tokens
        self.max_segundos = max_segundos
        self.max_chars_entrada = max_chars_entrada
        self.paralelo = max(1, paralelo)

    # ---- referencias

    @staticmethod
    def _resolver(ref: str, entrada: Dict, saidas: Dict):
        if ref.startswith('$entrada.'):
            return entrada.get(ref[len('$entrada.'):])
        partes = ref.split('.')
        if len(partes) == 3 and partes[0] == '$nos':
            return (saidas.get(partes[1]) or {}).get(partes[2])
        return None

    def _mapa(self, mapa: Dict, entrada, saidas) -> Dict[str, Any]:
        out = {}
        for nome, valor in (mapa or {}).items():
            for ref in _alternativas(valor):
                v = self._resolver(ref, entrada, saidas)
                if v is not None:
                    out[nome] = v
                    break
        return out

    @staticmethod
    def _avaliar(regra, entrada, saidas) -> bool:
        v = FlowRunner._resolver(regra.get('caminho') or '', entrada, saidas)
        s = ' '.join(map(str, v)) if isinstance(v, list) else ('true' if v is True else 'false' if v is False else str(v))
        alvo, op = str(regra.get('valor', '')), regra.get('op')
        try:
            if op == 'igual':
                return s == alvo
            if op == 'diferente':
                return s != alvo
            if op == 'existe':
                return v is not None
            if op == 'vazio':
                return v is None or v == '' or (isinstance(v, (list, dict)) and not v)
            if op == 'contem':
                return alvo in s
            if op == 'maior':
                return float(v) > float(alvo)
            if op == 'menor':
                return float(v) < float(alvo)
        except (TypeError, ValueError):
            return False
        return False

    # ---- nos

    def _texto_entradas(self, ent: Dict[str, Any]) -> str:
        blocos = []
        for k, v in ent.items():
            t = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
            if len(t) > self.max_chars_entrada:
                t = t[:self.max_chars_entrada] + f'\n[... cortado: {len(t) - self.max_chars_entrada} caracteres]'
            blocos.append(f'## {k}\n{t}')
        return '\n\n'.join(blocos)

    def _rodar_agente(self, no, ent, reg) -> Dict[str, Any]:
        slug = no.get('agente')
        ep = self.admin.get_effective_prompt(slug) if self.admin else None
        if not ep:
            raise IaNaoConfigurada(f'o agente "{slug}" nao esta vinculado ao produto')
        modelo = ep.get('model_name')
        if not modelo:
            raise IaNaoConfigurada(f'o agente "{slug}" nao tem modelo selecionado')
        s = no.get('saida') or {'formato': 'texto'}
        fmt = s.get('formato', 'texto')
        partes = [p for p in (ep.get('generic_content'), ep.get('custom_content')) if p]
        if no.get('instrucao'):
            partes.append('Instrucao desta etapa:\n' + no['instrucao'])
        if fmt == 'json':
            partes.append('Responda SOMENTE com um objeto JSON com os campos: ' + ', '.join(s.get('campos') or []) + '.')
        elif fmt == 'escolha':
            partes.append('Responda SOMENTE com um objeto JSON {"escolha": <uma de: '
                          + ', '.join(s.get('opcoes') or []) + '>, "motivo": <uma frase>}.')
        pedido = PedidoLLM(modelo=modelo, sistema='\n\n'.join(partes), usuario=self._texto_entradas(ent),
                           json=fmt in ('json', 'escolha'),
                           temperatura=ep.get('generic_temperature'), max_tokens=ep.get('generic_max_tokens'))
        reg['modelo'] = modelo
        for tentativa in (1, 2):
            resp = self.chamar_llm(pedido)
            reg['tokens_in'] += int(resp.tokens_entrada or 0)
            reg['tokens_out'] += int(resp.tokens_saida or 0)
            if self.admin:
                self.admin.track_token_usage(model_name=modelo, agent_slug=slug,
                                             prompt_tokens=int(resp.tokens_entrada or 0),
                                             completion_tokens=int(resp.tokens_saida or 0),
                                             endpoint_called=f"fluxo:{no['id']}")
            if fmt == 'texto':
                return {'texto': resp.texto}
            try:
                dado = json.loads(resp.texto)
                if not isinstance(dado, dict):
                    raise ValueError('nao e objeto')
                if fmt == 'escolha':
                    if dado.get('escolha') not in (s.get('opcoes') or []):
                        raise ValueError(f"escolha fora das opcoes: {dado.get('escolha')!r}")
                    return {'escolha': dado['escolha'], 'motivo': str(dado.get('motivo') or '')}
                faltam = [c for c in s.get('campos') or [] if c not in dado]
                if faltam:
                    raise ValueError('faltam os campos ' + ', '.join(faltam))
                return {c: dado[c] for c in s.get('campos') or []}
            except (ValueError, json.JSONDecodeError) as exc:
                if tentativa == 2:
                    raise FalhaDoNo(no['id'], f'o agente devolveu uma resposta fora do formato: {exc}')
                pedido.usuario += f'\n\n## Correcao\nA resposta anterior foi recusada ({exc}). Devolva no formato pedido.'
        raise FalhaDoNo(no['id'], 'sem resposta')  # pragma: no cover

    def _rodar_ferramenta(self, no, ent, ctx: ContextoFluxo, reg) -> Dict[str, Any]:
        f = self.ferramentas.get(no.get('ferramenta'))
        if f is None:
            raise FalhaDoNo(no['id'], f"o produto nao oferece mais a ferramenta \"{no.get('ferramenta')}\" (fluxo quebrado)")
        if no.get('versao') and int(no['versao']) != int(f.versao):
            raise FalhaDoNo(no['id'], f"a ferramenta \"{f.nome}\" mudou de versao ({no['versao']} -> {f.versao})")
        config = dict(no.get('config') or {})
        if f.efeito and ctx.modo == 'teste':
            reg['simulado'] = True
            return {'simulado': True, 'enviado': False,
                    'faria': {'ferramenta': f.nome, 'efeito': f.efeito, 'config': config, 'entradas': ent}}
        return f.funcao(ctx, ent, config) or {}

    # ---- execucao

    def run(self, definicao: Dict[str, Any], entrada: Dict[str, Any], modo: str = 'real',
            extra: Optional[Dict[str, Any]] = None, rotulo_versao: Optional[str] = None,
            ao_passo: Optional[Callable[[Dict[str, Any]], None]] = None) -> ResultadoFluxo:
        """Executa. Nunca levanta por falha de no: a falha vai no resultado.

        `ao_passo(registro)` e' chamado ao entrar e ao sair de cada no — para
        progresso ao vivo (SSE na tela, por exemplo).
        """
        if modo not in ('real', 'teste'):
            raise ValueError("modo deve ser 'real' ou 'teste'")
        g = _Grafo(definicao)
        inicio = time.time()
        pid = None
        if self.admin is not None:
            try:
                pid = self.admin._produto_efetivo()
            except Exception:  # pragma: no cover
                pid = None
        ctx = ContextoFluxo(modo=modo, entrada=dict(entrada), product_id=pid, extra=dict(extra or {}))
        res = ResultadoFluxo(saida=None)
        saidas: Dict[str, Dict[str, Any]] = {'entrada': ctx.entrada}
        st = {n: 'idle' for n in g.nos}
        st['entrada'] = 'ok'
        est: Dict[str, str] = {}
        voltas: Dict[str, int] = {}
        numero = [0]
        trava = threading.Lock()   # ramos paralelos numeram ao mesmo tempo

        def morrer(no_id):
            if st[no_id] != 'idle':
                return
            ch = g.chegam(no_id)
            if ch and all(est.get(e['id']) == 'dead' for e in ch):
                st[no_id] = 'dead'
                for e in g.saem(no_id):
                    est[e['id']] = 'dead'
                    if not e['max_voltas']:
                        morrer(e['para'])

        def disparar(e):
            alvo = e['para']
            if e['max_voltas']:
                voltas[e['id']] = voltas.get(e['id'], 0) + 1
                if voltas[e['id']] > int(e['max_voltas']):
                    raise LimiteExcedido(f"limite de {e['max_voltas']} volta(s) ao voltar para \"{g.nos[alvo].get('rotulo') or alvo}\"")
            est[e['id']] = 'fired'
            if st[alvo] in ('ok', 'dead'):
                st[alvo] = 'idle'
                for x in g.chegam(alvo):
                    if x['id'] != e['id'] and est.get(x['id']) == 'dead':
                        est.pop(x['id'], None)
            if e['max_voltas']:
                st[alvo] = 'forcado'

        def prontos():
            out = []
            for n, no in g.nos.items():
                if st[n] == 'forcado':
                    out.append(n)
                elif st[n] == 'idle' and no.get('tipo') != 'entrada':
                    ch = g.chegam(n)
                    if ch and all(est.get(e['id']) in ('fired', 'dead') for e in ch) \
                            and any(est.get(e['id']) == 'fired' for e in ch):
                        out.append(n)
            return out

        def assentar():
            mudou = False
            for n in g.nos:
                if st[n] != 'idle':
                    continue
                ch = g.chegam(n)
                if not any(est.get(e['id']) == 'fired' for e in ch):
                    continue
                for e in ch:
                    if e['id'] not in est and st[e['de']] not in ('idle', 'run', 'forcado'):
                        est[e['id']] = 'dead'
                        mudou = True
            return mudou

        def executar(no_id):
            """Roda UM no. Devolve (registro, porta). Sem efeito no estado do grafo."""
            no = g.nos[no_id]
            with trava:
                numero[0] += 1
                n_passo = numero[0]
            reg = {'n': n_passo, 'no': no_id, 'rotulo': no.get('rotulo') or no_id, 'tipo': no.get('tipo'),
                   'tokens_in': 0, 'tokens_out': 0, 'volta': sum(1 for e in g.chegam(no_id, True)
                                                                if e['max_voltas'] and voltas.get(e['id']))}
            if ao_passo:
                ao_passo(dict(reg, estado='rodando'))
            t0 = time.time()
            porta = 'out'
            tipo = no.get('tipo')
            detalhe = {'node_id': no_id, 'flow_version': rotulo_versao, 'volta': reg['volta']}
            passo_cm = nullcontext()
            if self.admin is not None and tipo in ('agente', 'ferramenta'):
                passo_cm = self.admin.agent_step(no.get('agente') if tipo == 'agente' else None,
                                                 label=reg['rotulo'], kind='llm' if tipo == 'agente' else 'step',
                                                 detail=detalhe)
            with passo_cm as passo:
                if tipo == 'agente':
                    ent = self._mapa(no.get('entradas'), ctx.entrada, saidas)
                    reg['entradas'] = ent
                    out = self._rodar_agente(no, ent, reg)
                    if (no.get('saida') or {}).get('formato') == 'escolha':
                        porta = out['escolha']
                elif tipo == 'ferramenta':
                    ent = self._mapa(no.get('entradas'), ctx.entrada, saidas)
                    reg['entradas'] = ent
                    out = self._rodar_ferramenta(no, ent, ctx, reg)
                elif tipo == 'condicao':
                    regra = next((r for r in no.get('regras') or [] if self._avaliar(r, ctx.entrada, saidas)), None)
                    porta = regra['ramo'] if regra else 'senao'
                    reg['decisao'] = [{'ramo': r.get('ramo'), 'caminho': r.get('caminho'), 'op': r.get('op'),
                                       'valor': r.get('valor'),
                                       'vale': self._avaliar(r, ctx.entrada, saidas)} for r in no.get('regras') or []]
                    out = {'ramo': porta}
                else:  # saida
                    out = self._mapa(no.get('campos'), ctx.entrada, saidas)
                if passo is not None and hasattr(passo, 'done'):
                    passo.done(tokens_in=reg['tokens_in'] or None, tokens_out=reg['tokens_out'] or None)
            reg['saida'] = out
            reg['ms'] = int((time.time() - t0) * 1000)
            if ao_passo:
                ao_passo(dict(reg, estado='ok'))
            return no_id, reg, out, porta

        def aplicar(no_id, reg, out, porta):
            saidas[no_id] = out
            res.traco.append(reg)
            res.tokens_entrada += reg['tokens_in']
            res.tokens_saida += reg['tokens_out']
            st[no_id] = 'ok'
            no = g.nos[no_id]
            if no.get('tipo') == 'saida':
                res.saida = out
                return
            ram = _ramificado(no)
            for e in g.saem(no_id):
                if ram and e['porta'] != porta:
                    est[e['id']] = 'dead'
                    if not e['max_voltas']:
                        morrer(e['para'])
            for e in g.saem(no_id):
                if not ram or e['porta'] == porta:
                    disparar(e)

        escopo = self.admin.execution_scope() if self.admin is not None else nullcontext(None)
        with escopo as corr:
            res.correlation_id = corr
            try:
                for e in g.saem('entrada'):
                    disparar(e)
                executados = 0
                with ThreadPoolExecutor(max_workers=self.paralelo) as pool:
                    while res.saida is None:
                        lote = prontos()
                        if not lote and assentar():
                            lote = prontos()
                        if not lote:
                            break
                        executados += len(lote)
                        if executados > self.max_nos:
                            raise LimiteExcedido(f'passou de {self.max_nos} nos executados')
                        for n in lote:
                            st[n] = 'run'
                        if len(lote) == 1:
                            resultados = [executar(lote[0])]
                        else:
                            # copy_context: o product_scope e o execution_scope vao
                            # junto para a thread — senao o passo do ramo cairia no pai.
                            futuros = [pool.submit(contextvars.copy_context().run, executar, n) for n in lote]
                            resultados = []
                            erro_lote = None
                            for f in futuros:
                                try:
                                    resultados.append(f.result())
                                except Exception as exc:  # o primeiro erro vence; os outros terminam
                                    erro_lote = erro_lote or exc
                            if erro_lote:
                                raise erro_lote
                        for r in sorted(resultados, key=lambda x: x[1]['n']):
                            aplicar(*r)
                        if self.max_tokens and res.tokens_entrada + res.tokens_saida > self.max_tokens:
                            raise LimiteExcedido(f'passou de {self.max_tokens} tokens na execucao')
                        if self.max_segundos and time.time() - inicio > self.max_segundos:
                            raise LimiteExcedido(f'passou de {self.max_segundos:g} s')
                if res.saida is None and res.falha is None:
                    res.falha = 'o fluxo terminou sem chegar a uma saida'
            except (FalhaDoNo, IaNaoConfigurada, LimiteExcedido) as exc:
                res.falha = str(exc)
                res.no_falha = getattr(exc, 'no_id', None) or next((n for n, s in st.items() if s == 'run'), None)
            except Exception as exc:  # erro da ferramenta ou do LLM: vira falha do no em curso
                logger.warning('FlowRunner: no falhou: %s', exc)
                res.falha = str(exc) or exc.__class__.__name__
                res.no_falha = next((n for n, s in st.items() if s == 'run'), None)
            if res.no_falha:
                res.traco.append({'n': numero[0] + 1, 'no': res.no_falha, 'tipo': 'erro',
                                  'rotulo': g.nos.get(res.no_falha, {}).get('rotulo') or res.no_falha,
                                  'erro': res.falha, 'tokens_in': 0, 'tokens_out': 0})
        res.duracao_ms = int((time.time() - inicio) * 1000)
        return res
