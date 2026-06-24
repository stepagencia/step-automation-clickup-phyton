"""
STEP — Ciclo de vida das tarefas de Planejamento de Conteúdo
=============================================================

Roda 1x/dia via GitHub Actions e aplica as transições de status nas
tarefas-ciclo (custom_item_id=1002) da lista "Planejamento de Conteúdo N1".

Máquina de estados (4 transições automáticas):
  1. próximos planejamentos → subir planejamento
       Gatilho: hoje >= dia 20 E CICLO da tarefa = mês daqui a 2 meses
       Ações:   trocar status, assignar Social Media do cliente,
                due_date = hoje + 7 dias (ajustado pra dia útil)

  2. aguardando input → executar calendário
       Gatilho: hoje >= 1º dia útil APÓS a Data do evento da
                Reunião de Input do mesmo cliente/mês
       Ações:   trocar status, assignar Social Media, due_date = hoje
                (já é dia útil pela construção)

  3. em produção (já na pauta) → ativo (vigente)
       Gatilho: hoje >= dia 1 do mês = CICLO
       Ações:   trocar status, remover assignees, remover due_date

  4. ativo (vigente) → concluido
       Gatilho: hoje >= último dia do mês = CICLO
       Ações:   trocar status

Manutenção (status mudado manualmente; o script só preenche a data):
  5. tarefa em "fazer/enviar doc input":
       Garantir due_date = (Data do evento da reunião do cliente/mês) − 7,
       ajustado pra dia útil. Mantém responsável.

  6. tarefa em "aguardando input":
       Garantir due_date vazio. Mantém responsável.

  7. tarefa em "em produção (já na pauta)": nada.

Idempotência: só faz update se valor atual != esperado.
DRY_RUN=true: só loga o que faria.
"""

from __future__ import annotations

import logging
import os
import sys
import time
import calendar
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

import requests

# ---------------------------------------------------------------------------
# Constantes — IDs do workspace da STEP
# ---------------------------------------------------------------------------

WORKSPACE_ID = "9013038195"

LIST_PLANEJAMENTO = "901306281641"     # tarefas-ciclo
LIST_REUNIOES = "901305872401"          # Reuniões & Agendamentos
LIST_GESTAO = "901301376959"            # Gestão do Especialista

# Filtro mestre
TASK_TYPE_PLANEJAMENTO = 1002

# Custom fields
CF_CLIENTE = "e41a916f-7818-44b6-9e93-fb003f52ad53"
CF_CICLO = "0ee5bf2a-f32d-4498-91f9-905aa2a0faf1"
CF_SOCIAL_MEDIA = "16d45d45-be3b-492d-a2ce-e396b2d0412e"
CF_TIPO_EVENTO = "209c5de5-dec2-40c1-ad91-e3d94b6a3221"
CF_DATA_EVENTO = "a58891f7-d8dc-4b0b-ace4-07efbacc0da8"

OPT_TIPO_EVENTO_INPUT = "4ee417dd-8b7b-4593-ab7a-62ffe2a729ae"

# Status (nomes exatos)
ST_PROXIMOS = "próximos planejamentos"
ST_SUBIR = "subir planejamento"
ST_FAZER_INPUT = "fazer/enviar doc input"
ST_AGUARDANDO_INPUT = "aguardando input"
ST_EXECUTAR_CAL = "executar calendário"
ST_EM_PRODUCAO = "em produção (já na pauta)"
ST_ATIVO = "ativo (vigente)"
ST_CONCLUIDO = "concluido"

MESES_PT_INDEX = {
    "JANEIRO": 1, "FEVEREIRO": 2, "MARÇO": 3, "ABRIL": 4,
    "MAIO": 5, "JUNHO": 6, "JULHO": 7, "AGOSTO": 8,
    "SETEMBRO": 9, "OUTUBRO": 10, "NOVEMBRO": 11, "DEZEMBRO": 12,
}

# Regex pra extrair [Mês/Ano] do nome da reunião (ex: "[Junho/2026]")
RE_MES_ANO = re.compile(r"\[\s*([A-Za-zÀ-ÿ]+)\s*/\s*(\d{2,4})\s*\]")

# ---------------------------------------------------------------------------
# Cliente ClickUp
# ---------------------------------------------------------------------------

log = logging.getLogger("planejamento_ciclo_sync")

API_BASE = "https://api.clickup.com/api/v2"


class ClickUp:
    """Wrapper sobre a API do ClickUp com retry e DRY_RUN."""

    def __init__(self, token: str, dry_run: bool = False) -> None:
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": token,
            "Content-Type": "application/json",
        })
        self.dry_run = dry_run

    def _req(self, method: str, path: str, *, write: bool = False,
             **kwargs: Any) -> Any:
        if write and self.dry_run:
            log.info("    [DRY_RUN] %s %s %s", method, path,
                     kwargs.get("json") or kwargs.get("params") or "")
            return {}
        for attempt in range(3):
            resp = self.session.request(method, f"{API_BASE}{path}",
                                        timeout=30, **kwargs)
            if resp.status_code == 429:
                wait = 2 ** attempt
                log.warning("Rate limit, aguardando %ss", wait)
                time.sleep(wait)
                continue
            # Retry em 5xx só pra leituras
            if 500 <= resp.status_code < 600 and not write and attempt < 2:
                wait = 2 ** attempt
                log.warning("ClickUp %s %s -> %s (5xx transient), retry em %ss",
                            method, path, resp.status_code, wait)
                time.sleep(wait)
                continue
            if resp.status_code >= 400:
                log.error("ClickUp %s %s -> %s: %s",
                          method, path, resp.status_code, resp.text[:500])
                resp.raise_for_status()
            return resp.json() if resp.text else None
        resp.raise_for_status()

    # ----- Leitura -----

    def list_field_options(self, list_id: str) -> dict:
        """Retorna {field_id: [opções...]} pra lista."""
        data = self._req("GET", f"/list/{list_id}/field") or {}
        out = {}
        for f in data.get("fields", []):
            opts = f.get("type_config", {}).get("options", []) or []
            out[f["id"]] = opts
        return out

    def list_statuses(self, list_id: str) -> list[dict]:
        data = self._req("GET", f"/list/{list_id}") or {}
        return data.get("statuses", []) or []

    def filter_team_tasks(self, list_ids: list[str], *,
                          custom_items: Optional[list[int]] = None,
                          include_closed: bool = True) -> list[dict]:
        """Filtered search com paginação."""
        out: list[dict] = []
        page = 0
        params: list[tuple[str, Any]] = [
            ("subtasks", "true"),
            ("include_closed", "true" if include_closed else "false"),
            ("archived", "false"),
        ]
        for lid in list_ids:
            params.append(("list_ids[]", lid))
        for ci in (custom_items or []):
            params.append(("custom_items[]", ci))
        while True:
            page_params = params + [("page", page)]
            data = self._req("GET", f"/team/{WORKSPACE_ID}/task",
                             params=page_params)
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1
        return out

    def list_tasks_simple(self, list_id: str,
                          include_closed: bool = True) -> list[dict]:
        """Pagina diretamente a lista (usado pra Gestão do Especialista
        que não permite filtered search por list_ids[])."""
        out: list[dict] = []
        page = 0
        while True:
            params = {
                "page": page,
                "subtasks": "true",
                "include_closed": "true" if include_closed else "false",
                "archived": "false",
                "order_by": "created",
                "reverse": "true",
            }
            data = self._req("GET", f"/list/{list_id}/task", params=params)
            tasks = data.get("tasks", []) if data else []
            out.extend(tasks)
            if len(tasks) < 100:
                break
            page += 1
        return out

    # ----- Escrita -----

    def update_task(self, task_id: str, payload: dict) -> dict:
        return self._req("PUT", f"/task/{task_id}",
                         json=payload, write=True) or {}

    def set_custom_field(self, task_id: str, field_id: str, value: Any) -> None:
        self._req("POST", f"/task/{task_id}/field/{field_id}",
                  json={"value": value}, write=True)

    def remove_assignee(self, task_id: str, user_id: int) -> None:
        # PUT /task/{id} com assignees: {"rem": [user_id]}
        self._req("PUT", f"/task/{task_id}",
                  json={"assignees": {"rem": [user_id]}}, write=True)

    def add_assignee(self, task_id: str, user_id: int) -> None:
        self._req("PUT", f"/task/{task_id}",
                  json={"assignees": {"add": [user_id]}}, write=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def cf_value(task: dict, field_id: str) -> Any:
    for cf in task.get("custom_fields", []):
        if cf["id"] == field_id:
            return cf.get("value")
    return None


def dropdown_option_id(task: dict, field_id: str) -> Optional[str]:
    """UUID da opção selecionada num dropdown."""
    for cf in task.get("custom_fields", []):
        if cf["id"] != field_id:
            continue
        val = cf.get("value")
        if val is None:
            return None
        options = cf.get("type_config", {}).get("options", [])
        if isinstance(val, int):
            if 0 <= val < len(options):
                return options[val]["id"]
            return None
        if isinstance(val, str):
            return val or None
    return None


def dropdown_option_name(task: dict, field_id: str,
                         options_cache: list[dict]) -> Optional[str]:
    """Nome da opção selecionada (resolve via cache de opções)."""
    opt_id = dropdown_option_id(task, field_id)
    if not opt_id:
        return None
    for o in options_cache:
        if o.get("id") == opt_id:
            return (o.get("name") or "").strip()
    return None


def users_field_first_id(task: dict, field_id: str) -> Optional[int]:
    """ID do primeiro usuário num campo type=users."""
    val = cf_value(task, field_id)
    if not val or not isinstance(val, list):
        return None
    if not val:
        return None
    u = val[0]
    if isinstance(u, dict) and "id" in u:
        try:
            return int(u["id"])
        except (TypeError, ValueError):
            return None
    return None


# --- Datas -------------------------------------------------------------------

def is_business_day(d: date) -> bool:
    """Seg-Sex. TODO: adicionar feriados quando for relevante."""
    return d.weekday() < 5


def next_business_day(d: date) -> date:
    """Se d cair sáb/dom, anda pra próxima segunda. Senão, mantém."""
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def date_to_ms(d: date) -> int:
    """Converte date pra unix ms (meia-noite UTC)."""
    return int(datetime(d.year, d.month, d.day,
                        tzinfo=timezone.utc).timestamp() * 1000)


def ms_to_date(ms: Any) -> Optional[date]:
    if ms is None or ms == "":
        return None
    try:
        ts = int(ms) / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc).date()
    except (TypeError, ValueError):
        return None


def add_months(year: int, month: int, delta: int) -> tuple[int, int]:
    """(ano, mês) deslocado em `delta` meses."""
    total = (year * 12 + (month - 1)) + delta
    return total // 12, (total % 12) + 1


def last_day_of_month(year: int, month: int) -> date:
    return date(year, month, calendar.monthrange(year, month)[1])


# --- Reunião → Cliente/Mês ---------------------------------------------------

def parse_mes_ano_from_name(name: str) -> Optional[tuple[int, int]]:
    """Extrai (mes, ano) do padrão '[Mês/Ano]' no nome da reunião."""
    m = RE_MES_ANO.search(name or "")
    if not m:
        return None
    mes_nome = m.group(1).strip().upper()
    mes_num = MESES_PT_INDEX.get(mes_nome)
    if not mes_num:
        return None
    ano = int(m.group(2))
    if ano < 100:
        ano += 2000
    return mes_num, ano


# ---------------------------------------------------------------------------
# Carregamento de caches
# ---------------------------------------------------------------------------

def carregar_caches(cu: ClickUp) -> dict:
    """Carrega tudo que precisamos em memória de uma vez:
      - statuses_planejamento: nome → id (apenas pra log/validação)
      - ciclo_options: lista de opções do dropdown CICLO
      - ciclo_name_to_id: 'AGOSTO' → uuid
      - reunioes_por_cliente_mes: {(cliente_opt_id, mes, ano): reuniao_task}
      - social_media_por_cliente: {cliente_opt_id: user_id}
    """
    cache: dict[str, Any] = {}

    # Statuses da lista de planejamento (só pra log)
    statuses = cu.list_statuses(LIST_PLANEJAMENTO)
    cache["statuses_planejamento"] = {
        (s.get("status") or "").strip().lower(): s for s in statuses
    }
    log.info("  Statuses da lista planejamento: %s",
             list(cache["statuses_planejamento"].keys()))

    # Opções do dropdown CICLO
    plan_field_opts = cu.list_field_options(LIST_PLANEJAMENTO)
    ciclo_opts = plan_field_opts.get(CF_CICLO, [])
    cache["ciclo_options"] = ciclo_opts
    cache["ciclo_id_to_name"] = {
        o["id"]: (o.get("name") or "").strip().upper() for o in ciclo_opts
    }
    cache["ciclo_name_to_id"] = {
        (o.get("name") or "").strip().upper(): o["id"] for o in ciclo_opts
    }
    log.info("  Opções CICLO: %s", list(cache["ciclo_name_to_id"].keys()))

    # Reuniões — filtradas por tipo "Reunião de Input"
    reunioes = cu.filter_team_tasks([LIST_REUNIOES], include_closed=True)
    log.info("  Reuniões na lista: %d", len(reunioes))

    reuniao_idx: dict[tuple[str, int, int], dict] = {}
    for r in reunioes:
        tipo_evento_id = dropdown_option_id(r, CF_TIPO_EVENTO)
        if tipo_evento_id != OPT_TIPO_EVENTO_INPUT:
            continue
        cliente_opt_id = dropdown_option_id(r, CF_CLIENTE)
        if not cliente_opt_id:
            continue
        mes_ano = parse_mes_ano_from_name(r.get("name") or "")
        if not mes_ano:
            continue
        mes, ano = mes_ano
        key = (cliente_opt_id, mes, ano)
        # Se duplicar, mantém a mais antiga (criada primeiro)
        if key not in reuniao_idx:
            reuniao_idx[key] = r
    cache["reunioes_idx"] = reuniao_idx
    log.info("  Reuniões de Input indexadas (cliente,mês,ano): %d",
             len(reuniao_idx))

    # Tarefas de Gestão do Especialista — pra achar Social Media por cliente
    gestao_tasks = cu.list_tasks_simple(LIST_GESTAO)
    log.info("  Tarefas de Gestão do Especialista: %d", len(gestao_tasks))

    sm_por_cliente: dict[str, int] = {}
    for g in gestao_tasks:
        cliente_opt_id = dropdown_option_id(g, CF_CLIENTE)
        if not cliente_opt_id:
            continue
        sm_uid = users_field_first_id(g, CF_SOCIAL_MEDIA)
        if not sm_uid:
            continue
        # Primeira ocorrência ganha (assume um especialista por cliente)
        sm_por_cliente.setdefault(cliente_opt_id, sm_uid)
    cache["social_media_por_cliente"] = sm_por_cliente
    log.info("  Social Media mapeado pra %d cliente(s)", len(sm_por_cliente))

    return cache


# ---------------------------------------------------------------------------
# Aplicação das transições
# ---------------------------------------------------------------------------

def aplicar_transicoes(cu: ClickUp, hoje: date, cache: dict) -> dict:
    """Itera todas as tarefas-ciclo (custom_item_id=1002) na lista de
    planejamento e aplica as 4 transições + 2 manutenções."""

    log.info("Buscando tarefas-ciclo (custom_item_id=%d) na lista %s...",
             TASK_TYPE_PLANEJAMENTO, LIST_PLANEJAMENTO)
    tarefas = cu.filter_team_tasks(
        [LIST_PLANEJAMENTO],
        custom_items=[TASK_TYPE_PLANEJAMENTO],
        include_closed=True,
    )
    log.info("  %d tarefa(s) de Planejamento encontrada(s)", len(tarefas))

    stats = {"t1": 0, "t2": 0, "t3": 0, "t4": 0,
             "m5": 0, "m6": 0, "skip": 0, "erro": 0}

    for task in tarefas:
        try:
            _processar_tarefa(cu, task, hoje, cache, stats)
        except Exception as exc:  # noqa: BLE001
            log.exception("Erro ao processar %s: %s",
                          task.get("custom_id") or task.get("id"), exc)
            stats["erro"] += 1

    return stats


def _processar_tarefa(cu: ClickUp, task: dict, hoje: date,
                      cache: dict, stats: dict) -> None:
    task_id = task["id"]
    name = task.get("name") or "(sem nome)"
    custom_id = task.get("custom_id") or task_id
    status_atual = ((task.get("status") or {}).get("status")
                    or "").strip().lower()

    # CICLO da tarefa
    ciclo_opt_id = dropdown_option_id(task, CF_CICLO)
    ciclo_nome = cache["ciclo_id_to_name"].get(ciclo_opt_id) if ciclo_opt_id else None
    ciclo_mes = MESES_PT_INDEX.get(ciclo_nome) if ciclo_nome else None

    cliente_opt_id = dropdown_option_id(task, CF_CLIENTE)
    due_atual_ms = task.get("due_date")

    log.debug("[%s] status=%r ciclo=%r cliente=%r",
              custom_id, status_atual, ciclo_nome, cliente_opt_id)

    # ===== TRANSIÇÃO 1 =====================================================
    # próximos planejamentos → subir planejamento
    # quando hoje >= 20 E ciclo == mês daqui a 2 meses
    if status_atual == ST_PROXIMOS:
        if hoje.day < 20 or not ciclo_mes:
            return
        ano_alvo, mes_alvo = add_months(hoje.year, hoje.month, 2)
        # CICLO é só mês (sem ano), então comparamos só o mês
        # mas usamos ano_alvo pra construir o ano-alvo correto.
        # Considera "match" quando o mês do ciclo == mes_alvo.
        if ciclo_mes != mes_alvo:
            return
        # Trigger! Calcula due_date = hoje + 7 dias, ajustado pra dia útil
        novo_due = next_business_day(hoje + timedelta(days=7))
        novo_due_ms = date_to_ms(novo_due)
        sm_uid = cache["social_media_por_cliente"].get(cliente_opt_id)

        log.info("[%s] T1: %s → %s (due=%s, sm=%s)",
                 custom_id, ST_PROXIMOS, ST_SUBIR, novo_due, sm_uid)
        cu.update_task(task_id, {
            "status": ST_SUBIR,
            "due_date": novo_due_ms,
            "due_date_time": False,
        })
        if sm_uid:
            cu.add_assignee(task_id, sm_uid)
        else:
            log.warning("[%s] Social Media não encontrado pro cliente %s — "
                        "status trocado, due setado, sem assignee",
                        custom_id, cliente_opt_id)
        stats["t1"] += 1
        return

    # ===== TRANSIÇÃO 2 =====================================================
    # aguardando input → executar calendário
    # quando hoje >= 1º dia útil APÓS Data do evento da reunião do cliente/mês
    if status_atual == ST_AGUARDANDO_INPUT:
        reuniao = _encontrar_reuniao(task, cache, ciclo_mes, hoje)
        if not reuniao:
            # mantém regra 6 (due vazio) - aplicada abaixo
            pass
        else:
            data_evento_ms = cf_value(reuniao, CF_DATA_EVENTO)
            data_evento = ms_to_date(data_evento_ms)
            if data_evento:
                gatilho = next_business_day(data_evento + timedelta(days=1))
                if hoje >= gatilho:
                    novo_due_ms = date_to_ms(hoje)
                    sm_uid = cache["social_media_por_cliente"].get(cliente_opt_id)
                    log.info("[%s] T2: %s → %s (due=%s, sm=%s)",
                             custom_id, ST_AGUARDANDO_INPUT, ST_EXECUTAR_CAL,
                             hoje, sm_uid)
                    cu.update_task(task_id, {
                        "status": ST_EXECUTAR_CAL,
                        "due_date": novo_due_ms,
                        "due_date_time": False,
                    })
                    if sm_uid:
                        cu.add_assignee(task_id, sm_uid)
                    else:
                        log.warning("[%s] Social Media não encontrado — sem assignee",
                                    custom_id)
                    stats["t2"] += 1
                    return
        # Não disparou T2 — aplica MANUTENÇÃO 6 (due vazio)
        if due_atual_ms not in (None, "", 0):
            log.info("[%s] M6: limpando due_date (status=aguardando input)",
                     custom_id)
            cu.update_task(task_id, {"due_date": None})
            stats["m6"] += 1
        return

    # ===== TRANSIÇÃO 3 =====================================================
    # em produção (já na pauta) → ativo (vigente)
    # quando hoje >= dia 1 do mês == CICLO
    if status_atual == ST_EM_PRODUCAO:
        if not ciclo_mes:
            return
        # Determina o ano do ciclo: assume ano atual; se ciclo já passou
        # nesse ano, é o próximo ano. (Heurística simples — mês passado
        # no calendário atual significa "ano que vem"; mas tarefas em
        # "em produção" não deveriam estar no passado.)
        ano_ciclo = _resolver_ano_do_ciclo(ciclo_mes, hoje)
        primeiro_dia_ciclo = date(ano_ciclo, ciclo_mes, 1)
        if hoje >= primeiro_dia_ciclo:
            log.info("[%s] T3: %s → %s (limpa assignees e due)",
                     custom_id, ST_EM_PRODUCAO, ST_ATIVO)
            payload: dict[str, Any] = {"status": ST_ATIVO, "due_date": None}
            # Remove todos os assignees atuais
            assignees = task.get("assignees") or []
            rem_ids = [int(a["id"]) for a in assignees if a.get("id")]
            if rem_ids:
                payload["assignees"] = {"rem": rem_ids}
            cu.update_task(task_id, payload)
            stats["t3"] += 1
        return

    # ===== TRANSIÇÃO 4 =====================================================
    # ativo (vigente) → concluido
    # quando hoje >= último dia do mês == CICLO
    if status_atual == ST_ATIVO:
        if not ciclo_mes:
            return
        ano_ciclo = _resolver_ano_do_ciclo(ciclo_mes, hoje)
        ultimo = last_day_of_month(ano_ciclo, ciclo_mes)
        if hoje >= ultimo:
            log.info("[%s] T4: %s → %s", custom_id, ST_ATIVO, ST_CONCLUIDO)
            cu.update_task(task_id, {"status": ST_CONCLUIDO})
            stats["t4"] += 1
        return

    # ===== MANUTENÇÃO 5 ===================================================
    # fazer/enviar doc input: due_date = data_evento − 7 (dia útil)
    if status_atual == ST_FAZER_INPUT:
        reuniao = _encontrar_reuniao(task, cache, ciclo_mes, hoje)
        if not reuniao:
            return
        data_evento = ms_to_date(cf_value(reuniao, CF_DATA_EVENTO))
        if not data_evento:
            log.info("[%s] M5: reunião do cliente/mês sem Data do evento — "
                     "pulando", custom_id)
            return
        due_alvo = next_business_day(data_evento - timedelta(days=7))
        due_alvo_ms = date_to_ms(due_alvo)
        if int(due_atual_ms or 0) != due_alvo_ms:
            log.info("[%s] M5: corrigindo due_date para %s", custom_id, due_alvo)
            cu.update_task(task_id, {
                "due_date": due_alvo_ms,
                "due_date_time": False,
            })
            stats["m5"] += 1
        return

    # ===== MANUTENÇÃO 7 ===================================================
    # em produção (já na pauta) sem T3 disparar: nada a fazer
    # (já cai no return acima quando T3 não dispara)

    stats["skip"] += 1


def _encontrar_reuniao(task: dict, cache: dict,
                       ciclo_mes: Optional[int],
                       hoje: date) -> Optional[dict]:
    """Encontra a reunião de Input do cliente + mês da tarefa."""
    cliente_opt_id = dropdown_option_id(task, CF_CLIENTE)
    if not cliente_opt_id or not ciclo_mes:
        return None
    ano_ciclo = _resolver_ano_do_ciclo(ciclo_mes, hoje)
    return cache["reunioes_idx"].get((cliente_opt_id, ciclo_mes, ano_ciclo))


def _resolver_ano_do_ciclo(ciclo_mes: int, hoje: date) -> int:
    """Heurística pra ano do CICLO (que vem só com nome do mês):
       - Se o mês do ciclo ainda não passou neste ano, é este ano.
       - Se já passou, é o ano que vem.
       - Mas se faltam >6 meses pro mês do ciclo neste ano, talvez
         seja ainda este ano (default). Mantemos simples: se ciclo_mes
         >= mes(hoje), é hoje.year; senão é hoje.year + 1."""
    if ciclo_mes >= hoje.month:
        return hoje.year
    return hoje.year + 1


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    token = os.environ.get("CLICKUP_API_TOKEN")
    if not token:
        log.error("CLICKUP_API_TOKEN ausente.")
        return 1

    dry_run = os.environ.get("DRY_RUN", "").lower() in {"1", "true", "yes"}
    if dry_run:
        log.info("*** MODO DRY_RUN: nenhuma escrita será feita ***")

    # Permite override da data via env (útil pra teste de transições futuras)
    hoje_str = os.environ.get("TODAY_OVERRIDE", "").strip()
    if hoje_str:
        try:
            hoje = datetime.strptime(hoje_str, "%Y-%m-%d").date()
            log.info("*** TODAY_OVERRIDE=%s ***", hoje)
        except ValueError:
            log.error("TODAY_OVERRIDE inválido (%r) — use YYYY-MM-DD", hoje_str)
            return 1
    else:
        hoje = datetime.now(timezone.utc).date()

    cu = ClickUp(token, dry_run=dry_run)

    log.info("=== Planejamento Ciclo Sync — %s ===", hoje)
    try:
        cache = carregar_caches(cu)
    except Exception as exc:  # noqa: BLE001
        log.exception("Erro fatal ao carregar caches: %s", exc)
        return 1

    stats = aplicar_transicoes(cu, hoje, cache)

    log.info("=" * 60)
    log.info("Resumo: T1=%d  T2=%d  T3=%d  T4=%d  M5=%d  M6=%d  skip=%d  erro=%d",
             stats["t1"], stats["t2"], stats["t3"], stats["t4"],
             stats["m5"], stats["m6"], stats["skip"], stats["erro"])
    return 0 if stats["erro"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
